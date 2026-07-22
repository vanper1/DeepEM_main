document.addEventListener('DOMContentLoaded', () => {
  const app = window.DeepEMApp;
  if (!app) return;
  if (typeof app.refresh === 'function') app.refresh();
  if (typeof app.bindEvents === 'function') app.bindEvents();
  if (typeof app.loadDeviceParamsToState === 'function') app.loadDeviceParamsToState();
  if (typeof app.refresh === 'function') setInterval(app.refresh, 1000);
});
