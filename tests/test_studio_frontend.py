from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_studio_frontend_is_pinned_and_build_artifacts_exist():
    package = (ROOT / "studio-frontend" / "package.json").read_text(encoding="utf-8")
    assert '"@assistant-ui/react-ag-ui": "0.0.57"' in package
    assert '"@ag-ui/client": "0.0.58"' in package
    assert '"vite": "7.3.7"' in package
    assert (ROOT / "app/web/static/studio-dist/assets/studio.js").exists()
    assert (ROOT / "app/web/static/studio-dist/assets/index.css").exists()


def test_studio_frontend_hides_reasoning_and_has_responsive_focus_language():
    source = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "studio-frontend/src/styles.css").read_text(encoding="utf-8")
    assert "showThinking: false" in source
    assert ":focus-visible" in styles
    assert "prefers-reduced-motion" in styles
    assert "@media (max-width: 680px)" in styles
    assert 'aria-label="Message the Studio agent"' in source
    assert 'aria-label="Send message"' in source
    assert "autoFocus" in source
    assert "overflow-x: hidden" in styles
    assert "env(safe-area-inset-bottom)" in styles
