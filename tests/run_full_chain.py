from numpy.testing import run_module_suite
from threadpoolctl import threadpool_limits
import sys

from test_simulator import *
from test_fullchain import *

if __name__ == "__main__":
    # Run tests
    args = sys.argv

    # If no args were specified, add arg to only do non-slow tests
    if len(args) == 1:
        print("Running tests that are not tagged as 'slow'. "
              "Use '--all' to run all tests.")
        args.append("-a!slow")

    with threadpool_limits(limits=1):
        run_module_suite(argv=args)
