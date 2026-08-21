"""The installable-web-app surface: manifest, service worker, offline page.

These are what make the app installable on a ward tablet, and what an Android
TWA wrapper reads if it is ever published to the Play Store.
"""
import json


def test_the_manifest_is_served_at_the_root(anon_client):
    response = anon_client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")


def test_the_manifest_describes_an_installable_app(anon_client):
    manifest = json.loads(anon_client.get("/manifest.webmanifest").text)
    assert manifest["name"] and manifest["short_name"]
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"

    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes, "Android requires both sizes to offer Install"
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])


def test_the_service_worker_is_served_from_the_root_scope(anon_client):
    """A worker served from /static could only control /static."""
    response = anon_client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["service-worker-allowed"] == "/"
    assert response.headers["cache-control"] == "no-cache"


def test_the_service_worker_never_caches_patient_data(anon_client):
    """The cache list is the whole defence — assert it holds no patient route."""
    source = anon_client.get("/sw.js").text
    for route in ("/cases", "/api", "/review", "/audit", "/users", "/upload"):
        assert f'"{route}' not in source, f"{route} must never be precached"
    assert '"/static/style.css"' in source
    assert '"/offline"' in source


def test_the_offline_page_works_without_a_session(anon_client):
    """It is shown when the server is unreachable, so it cannot need the server."""
    response = anon_client.get("/offline")
    assert response.status_code == 200
    assert "No connection" in response.text


def test_the_offline_page_does_not_pretend_to_have_data(anon_client):
    text = anon_client.get("/offline").text
    assert "out of date" in text
    assert "paper chart" in text


def test_icons_are_served(anon_client):
    for name in ("icon-192", "icon-512", "maskable-192", "apple-touch-icon"):
        response = anon_client.get(f"/static/icons/{name}.png")
        assert response.status_code == 200, name
        assert response.headers["content-type"] == "image/png"


def test_asset_links_404s_until_one_is_installed(anon_client):
    """Only needed for a Play Store wrapper; absent is correct otherwise."""
    assert anon_client.get("/.well-known/assetlinks.json").status_code == 404


def test_every_page_carries_the_install_metadata(client):
    page = client.get("/").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    assert 'name="theme-color"' in page
    assert 'navigator.serviceWorker.register("/sw.js")' in page
