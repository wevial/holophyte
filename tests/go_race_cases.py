"""Go's race detector in the reviewer image, collected by test_isolation."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import review_runner
from holophyte.isolation import launcher

RACE_TEST = """package race

import (
	"sync"
	"testing"
)

func TestTwoGoroutines(t *testing.T) {
	var mu sync.Mutex
	var wg sync.WaitGroup
	total := 0
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			mu.Lock()
			total++
			mu.Unlock()
		}()
	}
	wg.Wait()
	if total != 2 {
		t.Fatalf("total = %d", total)
	}
}
"""


class GoRaceCases:
    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_go_test_race_passes_with_two_goroutines(self):
        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        review_runner._ensure_image(
            review_runner.IMAGE, review_runner.DOCKERFILE.read_text(),
            candidate="working tree")
        with tempfile.TemporaryDirectory() as workspace:
            module = Path(workspace)
            (module / "go.mod").write_text("module example.com/race\n\ngo 1.26\n")
            (module / "race_test.go").write_text(RACE_TEST)
            code, output = launcher.launch(
                launcher.Route("container", writable=False), module, {},
                ["sh", "-c",
                 "export GOPATH=/tmp/go GOCACHE=/tmp/go-build GOTMPDIR=/tmp"
                 " && go test -race -count=1 ./..."],
                timeout=600)

        self.assertEqual(code, 0, output)
        self.assertIn("ok  \texample.com/race", output)
