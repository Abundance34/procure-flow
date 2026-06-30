# Blank Screen Startup Fix

## Fixed

The browser-refresh cookie bridge could call `st.stop()` while the third-party cookie component was still initializing. On Streamlit Community Cloud this could leave the page blank instead of rendering the login page or active workspace.

The cookie bridge is now non-blocking:

- The app always renders instead of stopping at startup.
- Session restoration remains enabled whenever the encrypted cookie component is ready.
- A newly created session token is held in memory until the cookie bridge is ready, then saved automatically.
- If the component is unavailable, the app falls back safely to normal login rather than showing a blank page.

No role workflow, page, approval rule, or Finance receipt behavior was changed.
