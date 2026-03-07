import ipaddress
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import network_policy as np


class NetworkPolicyTest(unittest.TestCase):
    def test_validate_llm_endpoint_allows_private_direct_route(self):
        with patch(
            "network_policy.resolve_hostname_ips",
            return_value=(ipaddress.ip_address("192.168.10.5"),),
        ):
            decision = np.validate_llm_endpoint(
                "http://host.docker.internal:8080/v1/chat/completions",
                allowlisted_hosts=("host.docker.internal",),
                no_proxy_hosts=("host.docker.internal",),
            )

        self.assertEqual(decision.host, "host.docker.internal")
        self.assertEqual(decision.route, "direct")
        self.assertTrue(decision.is_private_like)

    def test_validate_llm_endpoint_rejects_private_host_without_no_proxy(self):
        with patch(
            "network_policy.resolve_hostname_ips",
            return_value=(ipaddress.ip_address("192.168.10.5"),),
        ):
            with self.assertRaises(np.NetworkPolicyError) as context:
                np.validate_llm_endpoint(
                    "http://host.docker.internal:8080/v1/chat/completions",
                    allowlisted_hosts=("host.docker.internal",),
                    no_proxy_hosts=("localhost",),
                )
        self.assertIn("must be listed in WORKER_NO_PROXY", str(context.exception))

    def test_validate_llm_endpoint_rejects_public_host_in_no_proxy(self):
        with patch(
            "network_policy.resolve_hostname_ips",
            return_value=(ipaddress.ip_address("93.184.216.34"),),
        ):
            with self.assertRaises(np.NetworkPolicyError) as context:
                np.validate_llm_endpoint(
                    "https://api.example.com/v1/chat/completions",
                    allowlisted_hosts=("api.example.com",),
                    no_proxy_hosts=("api.example.com",),
                )
        self.assertIn("must not bypass the egress proxy", str(context.exception))

    def test_validate_llm_endpoint_rejects_metadata_destination(self):
        with self.assertRaises(np.NetworkPolicyError) as context:
            np.validate_llm_endpoint(
                "http://169.254.169.254/latest/meta-data",
                allowlisted_hosts=("169.254.169.254",),
                no_proxy_hosts=("169.254.169.254",),
            )
        self.assertIn("metadata", str(context.exception))

    def test_validate_public_http_url_rejects_private_address(self):
        with patch(
            "network_policy.resolve_hostname_ips",
            return_value=(ipaddress.ip_address("10.0.0.8"),),
        ):
            with self.assertRaises(np.NetworkPolicyError) as context:
                np.validate_public_http_url("https://internal.example.com/health", context="DEEP_HEALTH_GITHUB_URL")
        self.assertIn("private", str(context.exception))

    def test_load_squid_allowed_domains_parses_multiline_acl(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(
                "acl allowed_domains dstdomain \\\n"
                "  .github.com \\\n"
                "  api.example.com\n"
            )
            path = Path(handle.name)
        self.addCleanup(path.unlink)

        domains = np.load_squid_allowed_domains(path)

        self.assertEqual(domains, (".github.com", "api.example.com"))
        self.assertTrue(np.host_allowed_by_squid("api.example.com", domains))
        self.assertTrue(np.host_allowed_by_squid("docs.github.com", domains))


if __name__ == "__main__":
    unittest.main()
