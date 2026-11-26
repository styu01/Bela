#!/usr/bin/env python3
"""Run the Pinterest suite with an outer network guard, including old tests."""
import importlib.util
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode = True


def main():
    suite = unittest.TestSuite()
    root = Path(__file__).resolve().parent
    for index, name in enumerate(['pinterest-buffer-topup.test.py',
                                  'pinterest-buffer-topup-stress.test.py',
                                  'pinterest-schedule-config.test.py',
                                  'pinterest-order-plan.test.py',
                                  'pinterest-order-plan-holes.test.py']):
        spec = importlib.util.spec_from_file_location(f'pinterest_suite_{index}', root / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('NETWORK_DISABLED')), \
         patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('NETWORK_DISABLED')), \
         patch.object(socket, 'getaddrinfo', side_effect=AssertionError('NETWORK_DISABLED')):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
