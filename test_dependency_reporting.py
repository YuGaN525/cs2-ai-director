"""A frozen executable must never attempt to run itself as a pip interpreter."""
import unittest
from unittest.mock import patch
import cs2_ai_director as director


class DependencyReportingTests(unittest.TestCase):
    def test_frozen_missing_component_fails_without_launching_pip(self):
        row={'package':'cryptography','installed':False}
        with patch.object(director.sys,'frozen',True,create=True), \
                patch.object(director,'check_dependencies',return_value=[row]), \
                patch.object(director.subprocess,'Popen') as launch:
            messages=[]
            self.assertFalse(director.install_dependencies(messages.append))
            launch.assert_not_called()
            self.assertIn('官网',messages[0])


if __name__=='__main__':unittest.main()
