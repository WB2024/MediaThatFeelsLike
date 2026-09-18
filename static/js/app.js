/* Left/right image cycling on tile-grid galleries (see vibes/templates/vibes/_tiles.html).
 * Kept out of the tile's <a> navigation via preventDefault/stopPropagation. */
function mtflCycleTile(event, btn, dir) {
  event.preventDefault();
  event.stopPropagation();
  const tile = btn.closest(".tile");
  const urls = tile.dataset.images.split("|");
  const idx = (((tile.dataset.idx ? parseInt(tile.dataset.idx, 10) : 0) + dir) % urls.length + urls.length) % urls.length;
  tile.dataset.idx = idx;
  const img = tile.querySelector(".tile-img");
  img.removeAttribute("width");
  img.removeAttribute("height");
  img.src = urls[idx];
  const gal = tile.querySelector(".gal");
  if (gal) gal.textContent = (idx + 1) + " / " + urls.length;
  return false;
}
