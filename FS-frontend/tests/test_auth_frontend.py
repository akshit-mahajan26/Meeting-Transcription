"""Frontend auth: token handling in the API client, per-account caches, and the login/logout gate."""

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

FRONTEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, FRONTEND_DIR)

from services.api_client import MeetingIntelligenceClient  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

# services/__init__ re-exports the api_client singleton, which shadows the submodule name
api_module = importlib.import_module("services.api_client")
APP = os.path.join(FRONTEND_DIR, "app.py")
USER = {"id": "u-1", "email": "test@test.com"}


def _response(status, body):
    res = MagicMock()
    res.status_code = status
    res.json.return_value = body
    res.text = str(body)
    return res


# ------------------------------- api client -------------------------------

def test_login_success_returns_token_and_user():
    client = MeetingIntelligenceClient(base_url="http://backend")
    ok = _response(200, {"access_token": "tok", "token_type": "bearer", "user": USER})
    with patch("httpx.Client.post", return_value=ok) as post:
        res = client.login("test@test.com", "123456")
    assert res == {"success": True, "token": "tok", "user": USER}
    assert post.call_args.args[0] == "http://backend/api/v1/auth/login"


def test_login_and_signup_failures_surface_the_backend_message():
    client = MeetingIntelligenceClient(base_url="http://backend")
    with patch("httpx.Client.post", return_value=_response(401, {"detail": "Invalid email or password."})):
        assert client.login("a@b.co", "badpass") == {"success": False, "error": "Invalid email or password."}
    taken = _response(409, {"detail": "An account with this email already exists."})
    with patch("httpx.Client.post", return_value=taken):
        assert "already exists" in client.signup("a@b.co", "secret1")["error"]
    invalid = _response(422, {"detail": [{"msg": "Value error, Enter a valid email address."}]})
    with patch("httpx.Client.post", return_value=invalid):
        assert client.signup("nope", "secret1")["error"] == "Enter a valid email address."


def test_requests_carry_the_session_token_and_a_401_ends_the_session():
    client = MeetingIntelligenceClient(base_url="http://backend")
    session = {api_module.TOKEN_KEY: "tok-123"}
    with patch.object(api_module, "_session_state", return_value=session):
        with patch("httpx.Client.get", return_value=_response(200, {"meetings": [{"id": "m1"}]})) as get:
            assert client.list_meetings() == [{"id": "m1"}]
        assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer tok-123"}

        with patch("httpx.Client.get", return_value=_response(401, {"detail": "Not authenticated."})):
            assert client.list_meetings() == []
        assert api_module.TOKEN_KEY not in session and session[api_module.SESSION_EXPIRED_KEY] is True


def test_no_token_means_no_auth_header():
    client = MeetingIntelligenceClient(base_url="http://backend")
    with patch.object(api_module, "_session_state", return_value={}):
        assert client._auth_headers() == {}


# ------------------------------- caches are per account -------------------------------

def test_cached_data_is_keyed_by_account():
    from ui import state

    state.st.cache_data.clear()
    with patch.object(state.api_client, "list_meetings", side_effect=[[{"id": "A"}], [{"id": "B"}]]) as listing:
        assert state._meetings_for("user-a") == [{"id": "A"}]
        assert state._meetings_for("user-b") == [{"id": "B"}]  # not served user-a's cached result
        assert state._meetings_for("user-a") == [{"id": "A"}]  # still user-a's own cached result
    assert listing.call_count == 2
    state.st.cache_data.clear()


# ------------------------------- login gate (real app, simulated browser) -------------------------------

def _logged_out_app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    return at


def test_logged_out_visitor_only_sees_the_login_page():
    at = _logged_out_app()
    assert not at.exception
    labels = {t.label for t in at.text_input}
    assert {"Email", "Password", "Confirm password"} <= labels
    assert [t.label for t in at.tabs] == ["Log in", "Sign up"]
    assert "Signed in as" not in " ".join(c.value for c in at.caption)


def test_login_opens_the_app_and_logout_returns_to_login():
    with patch.object(api_module.api_client, "login", return_value={"success": True, "token": "tok", "user": USER}), \
         patch.object(api_module.api_client, "list_meetings", return_value=[]), \
         patch.object(api_module.api_client, "list_projects", return_value=[]):
        at = _logged_out_app()
        next(t for t in at.text_input if t.label == "Email" and t.key != "signup_email").set_value("test@test.com")
        next(t for t in at.text_input if t.label == "Password" and t.key != "signup_password").set_value("123456")
        at.button[0].click().run()
        assert not at.exception
        assert at.session_state["auth_token"] == "tok"
        assert any("test@test.com" in c.value for c in at.caption)

        next(b for b in at.button if b.key == "logout_button").click().run()
        assert not at.exception
        assert "auth_token" not in at.session_state and "auth_user" not in at.session_state
        assert {"Email", "Password"} <= {t.label for t in at.text_input}


def test_wrong_password_stays_on_login_with_an_error():
    bad = {"success": False, "error": "Invalid email or password."}
    with patch.object(api_module.api_client, "login", return_value=bad):
        at = _logged_out_app()
        next(t for t in at.text_input if t.label == "Email" and t.key != "signup_email").set_value("x@y.co")
        next(t for t in at.text_input if t.label == "Password" and t.key != "signup_password").set_value("wrongpw")
        at.button[0].click().run()
    assert [e.value for e in at.error] == ["Invalid email or password."]
    assert "auth_token" not in at.session_state


def test_signup_validates_before_calling_the_backend():
    with patch.object(api_module.api_client, "signup") as signup:
        at = _logged_out_app()
        by_key = {t.key: t for t in at.text_input}
        by_key["signup_email"].set_value("new@example.com")
        by_key["signup_password"].set_value("abcdef")
        by_key["signup_confirm"].set_value("different")
        at.button[1].click().run()
        assert any("do not match" in w.value for w in at.warning)
        signup.assert_not_called()


@pytest.mark.parametrize("page", ["overview", "assistant", "meetings", "ingest"])
def test_page_urls_do_not_bypass_the_login(page):
    at = AppTest.from_file(APP, default_timeout=30)
    at.query_params["page"] = page
    at.run()
    assert not at.exception
    assert {"Email", "Password"} <= {t.label for t in at.text_input}
