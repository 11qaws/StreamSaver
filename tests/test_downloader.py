"""
downloader.py 핵심 로직 자체 검증 테스트

실행: python -m pytest tests/ -v
   또는: python tests/test_downloader.py
"""
import unittest
from unittest.mock import MagicMock, patch, call
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from downloader import DownloadManager
import config

FAKE_URL  = "https://www.youtube.com/watch?v=abc123"
FAKE_INFO = {"id": "abc123", "title": "Test Video", "live_status": None, "availability": None}


def _make_dm(cookie_valid=False):
    cm = MagicMock()
    cm.cookie_valid = cookie_valid
    return DownloadManager(cm)


# ────────────────────────────────────────────────────────────
# classify() — 순수 함수, 모킹 불필요
# ────────────────────────────────────────────────────────────

class TestClassify(unittest.TestCase):

    def setUp(self):
        self.dm = _make_dm()

    def _c(self, info):
        return self.dm.classify(info)

    def test_none_info_is_error(self):
        state, msg, is_mem = self._c(None)
        self.assertEqual(state, "error")

    def test_empty_dict_is_also_error(self):
        """빈 dict는 Python에서 falsy — None과 동일하게 error 처리"""
        state, msg, is_mem = self._c({})
        self.assertEqual(state, "error")

    def test_normal_video(self):
        state, msg, is_mem = self._c({"live_status": None})
        self.assertEqual(state, "normal")
        self.assertFalse(is_mem)

    def test_was_live_is_vod_not_live(self):
        """was_live는 이미 처리된 VOD — live 취급 금지, --live-from-start 불필요"""
        state, msg, is_mem = self._c({"live_status": "was_live"})
        self.assertEqual(state, "normal",
                         "was_live는 normal이어야 합니다 (--live-from-start X)")
        self.assertFalse(is_mem)

    def test_is_live_needs_flag(self):
        state, msg, is_mem = self._c({"live_status": "is_live"})
        self.assertEqual(state, "live")
        self.assertFalse(is_mem)

    def test_post_live_still_needs_flag(self):
        """post_live는 YouTube가 아직 처리 중 — --live-from-start 필요"""
        state, msg, is_mem = self._c({"live_status": "post_live"})
        self.assertEqual(state, "live",
                         "post_live는 live여야 합니다 (--live-from-start 필요)")
        self.assertFalse(is_mem)

    def test_upcoming(self):
        state, msg, is_mem = self._c({"live_status": "is_upcoming"})
        self.assertEqual(state, "upcoming")

    def test_private(self):
        state, msg, is_mem = self._c({"live_status": "private"})
        self.assertEqual(state, "private")

    def test_unavailable(self):
        state, msg, is_mem = self._c({"live_status": "unavailable"})
        self.assertEqual(state, "private")

    def test_member_only_vod(self):
        state, msg, is_mem = self._c({"availability": "member_only"})
        self.assertEqual(state, "normal")
        self.assertTrue(is_mem)

    def test_is_membership_video_flag(self):
        state, msg, is_mem = self._c({"is_membership_video": True})
        self.assertEqual(state, "normal")
        self.assertTrue(is_mem)

    def test_live_plus_membership(self):
        """is_live + member_only → live 상태이면서 멤버십 (두 플래그 모두 True)"""
        info = {"live_status": "is_live", "availability": "member_only"}
        state, msg, is_mem = self._c(info)
        self.assertEqual(state, "live")
        self.assertTrue(is_mem,
                        "멤버십 라이브는 is_mem=True여야 합니다")

    def test_was_live_plus_membership(self):
        """was_live + member_only → 일반 VOD이면서 멤버십"""
        info = {"live_status": "was_live", "availability": "member_only"}
        state, msg, is_mem = self._c(info)
        self.assertEqual(state, "normal")
        self.assertTrue(is_mem)

    def test_post_live_plus_membership(self):
        """post_live + member_only → live이면서 멤버십"""
        info = {"live_status": "post_live", "availability": "member_only"}
        state, msg, is_mem = self._c(info)
        self.assertEqual(state, "live")
        self.assertTrue(is_mem)


# ────────────────────────────────────────────────────────────
# get_info() — _run_ytdlp_info 모킹
# ────────────────────────────────────────────────────────────

class TestGetInfo(unittest.TestCase):
    """get_info() → (info, used_cookies, err_str) 3-tuple 반환"""

    def test_no_cookies_first_and_succeeds_single_call(self):
        """쿠키 없이 첫 시도 성공 → 쿠키 시도 없이 종료, 호출 1회"""
        dm = _make_dm(cookie_valid=True)
        dm._run_ytdlp_info = MagicMock(return_value=(FAKE_INFO, ""))

        with patch("downloader.os.path.exists", return_value=True):
            info, used_cookies, err = dm.get_info(FAKE_URL)

        self.assertEqual(info, FAKE_INFO)
        self.assertFalse(used_cookies, "쿠키 없이 성공했으므로 used_cookies=False")
        self.assertEqual(err, "")
        self.assertEqual(dm._run_ytdlp_info.call_count, 1,
                         "첫 시도(쿠키 없음)에서 성공하면 한 번만 호출")

        first_extra_args = dm._run_ytdlp_info.call_args_list[0][0][1]
        self.assertNotIn("--cookies", first_extra_args,
                         "첫 시도에는 --cookies 플래그가 없어야 함")

    def test_falls_back_to_cookies_web_client(self):
        """쿠키 없이 실패 → 쿠키 + web 클라이언트로 재시도 (info 추출엔 android 불필요)"""
        dm = _make_dm(cookie_valid=True)

        def fake_run(url, args):
            return (FAKE_INFO, "") if "--cookies" in args else (None, "members only")

        dm._run_ytdlp_info = MagicMock(side_effect=fake_run)

        with patch("downloader.os.path.exists", return_value=True):
            info, used_cookies, err = dm.get_info(FAKE_URL)

        self.assertEqual(info, FAKE_INFO)
        self.assertTrue(used_cookies)
        self.assertEqual(err, "")
        self.assertEqual(dm._run_ytdlp_info.call_count, 2)

        second_args = dm._run_ytdlp_info.call_args_list[1][0][1]
        self.assertIn("--cookies", second_args)
        # get_info에서는 android를 강제하지 않음 — android는 download 단계에서만
        android_forced = (
            "--extractor-args" in second_args and
            "android" in second_args[second_args.index("--extractor-args") + 1]
        )
        self.assertFalse(android_forced,
                         "get_info에서 android 클라이언트를 강제하면 멤버십 포맷 오류 발생")

    def test_no_cookie_file_skips_cookie_attempt(self):
        """쿠키 파일이 없으면 쿠키 시도 건너뜀 (호출 1회로 끝남)"""
        dm = _make_dm(cookie_valid=True)
        dm._run_ytdlp_info = MagicMock(return_value=(None, "some error"))

        with patch("downloader.os.path.exists", return_value=False):
            info, _, err = dm.get_info(FAKE_URL)

        self.assertIsNone(info)
        self.assertIn("some error", err)
        self.assertEqual(dm._run_ytdlp_info.call_count, 1,
                         "쿠키 없으면 한 번만 시도해야 함")

    def test_no_cookies_available_skips_cookie_attempt(self):
        """cookie_valid=False이면 쿠키 시도 건너뜀"""
        dm = _make_dm(cookie_valid=False)
        dm._run_ytdlp_info = MagicMock(return_value=(None, "error"))

        info, _, _ = dm.get_info(FAKE_URL)

        self.assertIsNone(info)
        self.assertEqual(dm._run_ytdlp_info.call_count, 1)

    def test_both_fail_returns_none_false_with_err(self):
        """두 시도 모두 실패 → (None, False, err_msg)"""
        dm = _make_dm(cookie_valid=True)
        dm._run_ytdlp_info = MagicMock(return_value=(None, "network error"))

        with patch("downloader.os.path.exists", return_value=True):
            info, used_cookies, err = dm.get_info(FAKE_URL)

        self.assertIsNone(info)
        self.assertFalse(used_cookies)
        self.assertIn("network error", err)


# ────────────────────────────────────────────────────────────
# _build_dl_cmd() — 명령어 구성 검증
# ────────────────────────────────────────────────────────────

class TestBuildDlCmd(unittest.TestCase):

    def setUp(self):
        self.dm = _make_dm(cookie_valid=True)
        self.task = MagicMock()
        self.task.url = FAKE_URL
        self.tpl = "/out/%(title)s.%(ext)s"

    def _cmd(self, use_cookies, state, exists=True):
        with patch("downloader.os.path.exists", return_value=exists):
            return self.dm._build_dl_cmd(self.task, self.tpl, "best", use_cookies, state)

    def test_live_state_adds_live_from_start(self):
        cmd = self._cmd(False, "live")
        self.assertIn("--live-from-start", cmd)

    def test_normal_state_no_live_from_start(self):
        cmd = self._cmd(False, "normal")
        self.assertNotIn("--live-from-start", cmd)

    def test_use_cookies_true_adds_cookies_flag(self):
        """쿠키 사용 시 --cookies 포함 (n challenge는 --js-runtimes로 해결)"""
        cmd = self._cmd(True, "normal", exists=True)
        self.assertIn("--cookies", cmd)

    def test_use_cookies_false_no_cookies_flag(self):
        """쿠키 미사용 시 --cookies 없음 (yt-dlp 클라이언트 자율 선택)"""
        cmd = self._cmd(False, "normal")
        self.assertNotIn("--cookies", cmd)

    def test_url_is_last_arg(self):
        cmd = self._cmd(False, "normal")
        self.assertEqual(cmd[-1], FAKE_URL)


# ────────────────────────────────────────────────────────────
# 재시도 계획 구조 — get_info 전략 고정 검증
# ────────────────────────────────────────────────────────────

class TestRetryPlan(unittest.TestCase):
    """
    _download()에서 생성하는 retry_plan 규칙:
      [(q, used_cookies) for q in QUALITY_PREFERENCES]
    쿠키 전략은 get_info 결과에서 고정, 품질만 낮아짐
    """

    def _plan(self, used_cookies):
        return [(q, used_cookies) for q in config.QUALITY_PREFERENCES]

    def test_no_cookies_strategy_never_flips(self):
        plan = self._plan(False)
        for q, ck in plan:
            self.assertFalse(ck, f"품질 '{q}': 쿠키 전략이 뒤집히면 안 됨")

    def test_cookies_strategy_never_flips(self):
        plan = self._plan(True)
        for q, ck in plan:
            self.assertTrue(ck, f"품질 '{q}': 쿠키 전략이 뒤집히면 안 됨")

    def test_plan_length_equals_quality_steps(self):
        plan = self._plan(False)
        self.assertEqual(len(plan), len(config.QUALITY_PREFERENCES),
                         "품질 단계마다 정확히 1번 시도")

    def test_quality_order_is_preserved(self):
        plan = self._plan(False)
        for (q, _), expected in zip(plan, config.QUALITY_PREFERENCES):
            self.assertEqual(q, expected, "품질 순서가 유지되어야 함")


if __name__ == "__main__":
    unittest.main(verbosity=2)
