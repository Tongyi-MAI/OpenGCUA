// Keep bandwidth and audio focused on the video the visitor chooses to play.
document.addEventListener('play', event => {
  if (!event.target.matches('.release-demos video')) return;
  document.querySelectorAll('.release-demos video').forEach(video => {
    if (video !== event.target) video.pause();
  });
}, true);
