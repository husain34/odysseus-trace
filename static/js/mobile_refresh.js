// Local-only Mobile App Refresher
// This file is ignored by git and automatically reloads the chat when you return to the app

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    if (window.sessionModule && typeof window.sessionModule.loadSessions === 'function') {
      console.log("[MobileRefresh] App gained focus. Reloading sessions to fetch missed messages.");
      window.sessionModule.loadSessions().catch(e => console.error(e));
    }
  }
});
