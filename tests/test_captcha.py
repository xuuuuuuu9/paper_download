import threading
import unittest

from scihub.captcha import CaptchaCoordinator


class CaptchaCoordinatorTests(unittest.TestCase):
    def test_same_mirror_concurrent_requests_share_one_browser_solve(self):
        calls = []

        def fake_solver(url, mirror, cookies):
            calls.append((url, mirror, cookies))
            return {"ok": "1"}

        coordinator = CaptchaCoordinator(interactive=True, solver=fake_solver)
        barrier = threading.Barrier(3)
        results = []

        def request():
            barrier.wait()
            results.append(coordinator.solve("sci-hub.st", "https://sci-hub.st/10.test", {"a": "b"}))

        threads = [threading.Thread(target=request) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(calls, [("https://sci-hub.st/10.test", "sci-hub.st", {"a": "b"})])
        self.assertEqual(results, [{"ok": "1"}, {"ok": "1"}])

    def test_non_interactive_returns_empty_without_browser_solve(self):
        def fake_solver(url, mirror, cookies):
            raise AssertionError("solver should not be called")

        coordinator = CaptchaCoordinator(interactive=False, solver=fake_solver)

        self.assertEqual(coordinator.solve("sci-hub.st", "https://sci-hub.st/10.test", {}), {})


if __name__ == "__main__":
    unittest.main()
