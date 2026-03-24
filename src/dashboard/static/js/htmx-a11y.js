/**
 * HTMX accessibility helpers.
 * - Sets aria-busy="true" on swap targets during requests.
 * - Removes aria-busy after swap settles.
 */
document.addEventListener("htmx:beforeRequest", function (evt) {
  var target = evt.detail.target;
  if (target) target.setAttribute("aria-busy", "true");
});

document.addEventListener("htmx:afterSettle", function (evt) {
  var target = evt.detail.target;
  if (target) target.removeAttribute("aria-busy");
});
