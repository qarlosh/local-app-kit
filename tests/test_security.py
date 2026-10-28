import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from local_app_kit.security import CSP, SecurityMiddleware, allowed_hosts


def make_client(hosts=allowed_hosts("local"), base_url="http://127.0.0.1:7700"):
    app = FastAPI()
    app.add_middleware(SecurityMiddleware, hosts=hosts)

    @app.post("/api/things")
    def create():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_text("hi")
        await socket.close()

    return TestClient(app, base_url=base_url)


def test_curl_without_origin_passes():
    response = make_client().post("/api/things")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == CSP


def test_own_page_passes():
    headers = {"Origin": "http://127.0.0.1:7700", "Sec-Fetch-Site": "same-origin"}
    assert make_client().post("/api/things", headers=headers).status_code == 200


def test_foreign_origin_is_rejected():
    response = make_client().post("/api/things", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.headers["content-security-policy"] == CSP


def test_null_origin_is_rejected():
    assert make_client().post("/api/things", headers={"Origin": "null"}).status_code == 403


def test_cross_site_fetch_is_rejected():
    # A browser may leave Origin out (a GET, say), but still says where the request comes from.
    response = make_client().post("/api/things", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403


def test_fake_host_is_rejected():
    # DNS rebinding: evil.example resolves to 127.0.0.1, so the browser sends its own name as Host.
    response = make_client().post("/api/things", headers={"Host": "evil.example:7700"})
    assert response.status_code == 403


@pytest.mark.parametrize("host", ["localhost:7700", "[::1]:7700", "127.0.0.1"])
def test_local_names_pass(host):
    assert make_client().post("/api/things", headers={"Host": host}).status_code == 200


def test_public_exposure_adds_the_tunnel_domain():
    hosts = allowed_hosts("public", "my-app.ngrok.app")
    client = make_client(hosts, base_url="https://my-app.ngrok.app")
    assert client.post("/api/things", headers={"Origin": "https://my-app.ngrok.app"}).status_code == 200
    assert client.post("/api/things", headers={"Host": "127.0.0.1:7700"}).status_code == 200
    assert client.post("/api/things", headers={"Origin": "https://evil.example"}).status_code == 403


def test_local_exposure_rejects_the_tunnel_domain():
    response = make_client().post("/api/things", headers={"Host": "my-app.ngrok.app"})
    assert response.status_code == 403


def test_bad_exposure_settings():
    with pytest.raises(ValueError):
        allowed_hosts("public")
    with pytest.raises(ValueError):
        allowed_hosts("lan")


def test_websocket_from_foreign_origin_is_closed():
    url, headers = "ws://127.0.0.1:7700/ws", {"Origin": "https://evil.example"}
    with pytest.raises(WebSocketDisconnect) as closed:
        with make_client().websocket_connect(url, headers=headers) as socket:
            socket.receive_text()
    assert (closed.value.code, closed.value.reason) == (1008, "origin not allowed")


def test_websocket_from_own_page_opens():
    with make_client().websocket_connect("ws://127.0.0.1:7700/ws", headers={"Origin": "http://127.0.0.1:7700"}) as socket:
        assert socket.receive_text() == "hi"
