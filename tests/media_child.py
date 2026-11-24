"""Child process entry for the integration fixture using deterministic scene authoring."""
import sys
from pathlib import Path

from test_production import author_fixture

from film_worker.production import Production, run_child

if __name__=="__main__":
    Production.author = author_fixture
    raise SystemExit(run_child(Path(sys.argv[1])))
