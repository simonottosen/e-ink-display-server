import pytest

from inky_host.icloud import (
    IcloudDerivative,
    IcloudPhoto,
    IcloudShareError,
    _coerce_int,
    base_url_for,
    parse_share_token,
    require_allowed_url,
)


def test_parse_share_token_accepts_both_link_shapes() -> None:
    assert parse_share_token("https://share.icloud.com/photos/#B1aBcDeFgH") == "B1aBcDeFgH"
    assert parse_share_token("https://share.icloud.com/photos/B1aBcDeFgH") == "B1aBcDeFgH"
    assert parse_share_token("  B1aBcDeFgH  ") == "B1aBcDeFgH"


def test_parse_share_token_accepts_a_current_base64url_token() -> None:
    """A link copied from Photos today carries "-" and "_", not just base62."""

    # Shape copied from a real link, contents invented: the token is a bearer capability.
    token = "D0000000000000000000000000-00000000000ABCDEFGHJKLMNP_0000000000000000000000000"
    assert parse_share_token(f"https://www.icloud.com/sharedalbum/#{token}") == token
    assert base_url_for(token).startswith("https://p14-sharedstreams.icloud.com/")


@pytest.mark.parametrize("value", ["", "   ", "https://share.icloud.com/photos/not a token"])
def test_parse_share_token_rejects_malformed_input(value: str) -> None:
    with pytest.raises(IcloudShareError):
        parse_share_token(value)


@pytest.mark.parametrize(
    ("token", "partition"),
    [("A0xxxx", 11), ("a0xxxx", 37), ("z0xxxx", 22), ("00xxxx", 1)],
)
def test_base_url_for_matches_apples_partitioning(token: str, partition: int) -> None:
    assert base_url_for(token) == f"https://p{partition:02d}-sharedstreams.icloud.com/{token}/sharedstreams/"


def test_coerce_int_reads_numbers_sent_as_strings() -> None:
    assert _coerce_int("1234") == 1234
    assert _coerce_int(1234) == 1234
    assert _coerce_int("wide") is None
    assert _coerce_int(True) is None


def test_best_derivative_prefers_the_largest_image() -> None:
    small = IcloudDerivative(checksum="small", file_size=10, width=100, height=100)
    large = IcloudDerivative(checksum="large", file_size=5, width=400, height=400)
    photo = IcloudPhoto(photo_guid="g", caption=None, date_created=None, derivatives=(small, large))
    assert photo.best_derivative() is large
    assert IcloudPhoto("g", None, None, ()).best_derivative() is None


def test_require_allowed_url_accepts_apple_hosts() -> None:
    url = "https://cvws.icloud-content.com/S/abc?x=1"
    assert require_allowed_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://p11-sharedstreams.icloud.com/x",
        "https://192.168.1.1/x",
        "https://evil.com/x",
        "https://p11-sharedstreams.icloud.com:8080/x",
        "https://evil.com/@p11-sharedstreams.icloud.com/x",
    ],
)
def test_require_allowed_url_refuses_anything_outside_apple(url: str) -> None:
    with pytest.raises(IcloudShareError):
        require_allowed_url(url)
