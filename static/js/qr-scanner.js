(() => {
  const dialog = document.querySelector('[data-qr-scanner]');
  const decode = window.jsQR;
  if (!dialog?.showModal || typeof decode !== 'function') return;

  const launch = document.querySelector('[data-open-qr-scanner]');
  const video = dialog.querySelector('video');
  const status = dialog.querySelector('[data-qr-status]');
  const fileInput = dialog.querySelector('input[type="file"]');
  const target = new URL(dialog.dataset.scanUrl, location.href);
  const [prefix, suffix] = target.pathname.split('qr-record-id');
  const isBook = dialog.dataset.scanKind === 'book';
  const recordPattern = isBook ? /^LV-[0-9a-f]{16}$/i : /^\d+:[\w-]+$/;
  const label = isBook ? 'book label' : 'student pass';
  const canvas = document.createElement('canvas');
  const context = canvas.getContext('2d', {willReadFrequently: true});
  if (!context) return;

  let session;
  let stream;
  let timer;

  function stop() {
    session?.abort();
    clearInterval(timer);
    stream?.getTracks().forEach(track => track.stop());
    stream = undefined;
    video.srcObject = null;
    video.hidden = true;
  }

  function recordPath(value) {
    try {
      const url = new URL(value, location.origin);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return;
      if (url.origin !== target.origin && url.origin !== location.origin) return;
      if (!url.pathname.startsWith(prefix) || !url.pathname.endsWith(suffix)) return;
      const encoded = url.pathname.slice(prefix.length, url.pathname.length - suffix.length);
      const identifier = decodeURIComponent(encoded);
      if (recordPattern.test(identifier)) return prefix + identifier + suffix;
    } catch {
      return;
    }
  }

  function scan(source, width, height, maxSize) {
    const scale = Math.min(1, maxSize / Math.max(width, height));
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    context.drawImage(source, 0, 0, canvas.width, canvas.height);
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
    const result = decode(pixels.data, pixels.width, pixels.height);
    if (!result) return false;

    const path = recordPath(result.data);
    if (!path) {
      status.textContent = `Use a LibraVerse ${label} QR code.`;
      return true;
    }
    stop();
    status.textContent = 'Opening record…';
    location.assign(path);
    return true;
  }

  launch.addEventListener('click', async () => {
    session = new AbortController();
    const {signal} = session;
    fileInput.value = '';
    dialog.showModal();
    status.textContent = 'Starting camera…';
    try {
      const camera = await navigator.mediaDevices.getUserMedia({
        video: {facingMode: {ideal: 'environment'}, width: {ideal: 960}},
        audio: false,
      });
      if (signal.aborted) {
        camera.getTracks().forEach(track => track.stop());
        return;
      }
      stream = camera;
      video.srcObject = camera;
      video.hidden = false;
      await video.play();
      if (signal.aborted) return;
      status.textContent = `Point the camera at the ${label} QR code.`;
      timer = setInterval(() => {
        if (video.videoWidth && video.videoHeight) {
          scan(video, video.videoWidth, video.videoHeight, 720);
        }
      }, 200);
    } catch {
      if (signal.aborted) return;
      stream?.getTracks().forEach(track => track.stop());
      stream = undefined;
      video.srcObject = null;
      video.hidden = true;
      status.textContent = 'Camera unavailable. Choose an image with the QR code below.';
    }
  });

  fileInput.addEventListener('change', async () => {
    const [file] = fileInput.files;
    if (!file) return;
    const {signal} = session;
    const objectUrl = URL.createObjectURL(file);
    try {
      const image = new Image();
      image.src = objectUrl;
      await image.decode();
      if (!signal.aborted && !scan(image, image.naturalWidth, image.naturalHeight, 1280)) {
        status.textContent = 'No QR code found. Choose a clearer image.';
      }
    } catch {
      if (!signal.aborted) status.textContent = 'Could not read that image.';
    } finally {
      URL.revokeObjectURL(objectUrl);
    }
  });

  dialog.querySelector('.dialog-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', stop);
  addEventListener('pagehide', stop);
  launch.hidden = false;
})();
