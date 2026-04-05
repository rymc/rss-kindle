from app.cdp_proxy import _rewrite_http_host_header, _rewrite_json_discovery_response


def test_rewrite_http_host_header_replaces_named_host():
    payload = (
        b"GET /json/version HTTP/1.1\r\n"
        b"Host: browser-cdp:9223\r\n"
        b"Origin: http://browser-cdp:9223\r\n"
        b"User-Agent: test\r\n"
        b"\r\n"
    )

    rewritten = _rewrite_http_host_header(payload, target_host="127.0.0.1", target_port=9222)

    assert b"Host: 127.0.0.1:9222\r\n" in rewritten
    assert b"Origin: http://127.0.0.1:9222\r\n" in rewritten
    assert b"Host: browser-cdp:9223\r\n" not in rewritten


def test_rewrite_http_host_header_leaves_non_http_payload_alone():
    payload = b"\x81\x8ewebsocket-binary-frame"

    rewritten = _rewrite_http_host_header(payload, target_host="127.0.0.1", target_port=9222)

    assert rewritten == payload


def test_rewrite_json_discovery_response_rewrites_websocket_url_and_length():
    body = b'{ "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc" }'
    payload = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: 51\r\n"
        b"Content-Type: application/json\r\n"
        b"\r\n"
        + body
    )

    rewritten = _rewrite_json_discovery_response(
        payload,
        public_host="browser-cdp",
        public_port=9223,
        target_host="127.0.0.1",
        target_port=9222,
    )

    assert b'ws://browser-cdp:9223/devtools/browser/abc' in rewritten
    assert b'ws://127.0.0.1:9222/devtools/browser/abc' not in rewritten
    expected_body = b'{ "webSocketDebuggerUrl": "ws://browser-cdp:9223/devtools/browser/abc" }'
    assert f"Content-Length: {len(expected_body)}".encode("ascii") + b"\r\n" in rewritten
