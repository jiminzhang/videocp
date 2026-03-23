from videocp.input_parser import extract_first_url, parse_input


def test_extract_first_url_from_share_text():
    text = "5.18 复制打开抖音，看看【测试】 https://v.douyin.com/AbCdEfG/ 09/01"
    assert extract_first_url(text) == "https://v.douyin.com/AbCdEfG/"


def test_extract_first_url_trims_trailing_punctuation():
    text = "链接：https://www.douyin.com/video/1234567890）"
    assert extract_first_url(text) == "https://www.douyin.com/video/1234567890"


def test_parse_input_keeps_original_when_resolution_fails(monkeypatch):
    monkeypatch.setattr("videocp.input_parser.resolve_url", lambda url, timeout_secs=15: url)
    parsed = parse_input("https://www.douyin.com/video/1234567890")
    assert parsed.extracted_url == "https://www.douyin.com/video/1234567890"
    assert parsed.canonical_url == "https://www.douyin.com/video/1234567890"


def test_parse_input_canonicalizes_douyin_jingxuan_modal(monkeypatch):
    monkeypatch.setattr("videocp.input_parser.resolve_url", lambda url, timeout_secs=15: url)
    parsed = parse_input("https://www.douyin.com/jingxuan?modal_id=7617405320117128502")
    assert parsed.canonical_url == "https://www.douyin.com/video/7617405320117128502"
