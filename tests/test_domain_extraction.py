"""Domains that start with 'w' must survive www-prefix stripping.

str.lstrip("www.") strips *characters*, so "willreed.com" became
"illreed.com" and discovery probed a garbage domain.
"""
import job_scraper as js


def test_extract_domain_keeps_leading_w():
    assert js.extract_domain("https://willreed.com") == "willreed.com"
    assert js.extract_domain("wework.com") == "wework.com"


def test_extract_domain_strips_real_www_prefix():
    assert js.extract_domain("https://www.willreed.com") == "willreed.com"
    assert js.extract_domain("http://www.acme.io/about") == "acme.io"


def test_get_domain_keeps_leading_w():
    assert js.get_domain("https://wandb.ai/site") == "wandb.ai"
    assert js.get_domain("https://www.wandb.ai/site") == "wandb.ai"
