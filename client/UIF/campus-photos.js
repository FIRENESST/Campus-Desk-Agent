const maxUploadMb = 20;
const maxUpload = maxUploadMb * 1024 * 1024;
const galleryPath = '/v1/campus-view/photo-places/';
const imagePath = id => '/v1/campus-view/photos/' + encodeURIComponent(id) + '/image';
const formatDate = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' });
const formatSize = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 });

export function createCampusPhotoGallery(element, request, panelSignal, unavailable) {
  const section = element('section', 'map-photo-gallery');
  const heading = element('h3', '', '地点照片');
  const status = element('p', 'map-photo-status');
  status.setAttribute('role', 'status');
  const viewer = element('div', 'map-photo-viewer');
  viewer.hidden = true;
  const image = element('img');
  image.alt = '该地点的校园照片';
  image.loading = 'lazy';
  viewer.append(image);
  const photos = element('div', 'map-photo-strip');
  const more = element('button', 'outline-button', '加载更多照片');
  more.type = 'button';
  more.hidden = true;
  const form = element('form', 'map-photo-form');
  const label = element('label', 'panel-select', `为此地点投稿照片（每张最多 ${maxUploadMb} MB）`);
  const file = element('input');
  file.type = 'file';
  file.accept = 'image/jpeg,image/png,image/webp';
  file.required = true;
  label.append(file);
  const submit = element('button', 'outline-button', '提交审核');
  submit.type = 'submit';
  form.append(label, submit);
  const draft = element('div', 'map-photo-draft');
  draft.hidden = true;
  const draftImage = element('img');
  draftImage.alt = '待投稿照片预览';
  const draftText = element('div');
  const draftName = element('strong');
  const draftSize = element('small');
  draftText.append(draftName, draftSize);
  draft.append(draftImage, draftText);
  const publication = element('p', 'map-photo-hint', '通过审核后，能访问校园地图的用户即可查看该照片。');
  const privacy = element('a', '', '投稿隐私说明');
  privacy.href = '/privacy#campus';
  privacy.target = '_blank';
  privacy.rel = 'noopener';
  publication.append(privacy);
  form.append(draft, publication);
  const feedback = element('p', 'map-photo-status');
  feedback.setAttribute('role', 'status');
  section.append(heading, status, viewer, photos, more, form, feedback);
  let current;
  let uploadKey;
  let previewUrl;

  function releaseDraftUrl() {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = undefined;
  }

  function clearDraft() {
    draft.hidden = true;
    draftImage.removeAttribute('src');
    releaseDraftUrl();
    draftName.textContent = draftSize.textContent = '';
  }

  function validSize(upload) {
    return upload.size > 0 && upload.size <= maxUpload;
  }

  function showDraft() {
    clearDraft();
    uploadKey = undefined;
    const selected = file.files[0];
    submit.disabled = !selected || !validSize(selected);
    feedback.textContent = '';
    if (!selected) return;
    if (!validSize(selected)) {
      feedback.textContent = `请选择一张不超过 ${maxUploadMb} MB 的照片。`;
      return;
    }
    previewUrl = URL.createObjectURL(selected);
    draftImage.hidden = false;
    draftImage.src = previewUrl;
    draftName.textContent = selected.name;
    draftSize.textContent = selected.size < 1024 * 1024
      ? `${formatSize.format(selected.size / 1024)} KB` : `${formatSize.format(selected.size / (1024 * 1024))} MB`;
    draft.hidden = false;
  }

  function showPhoto(button, photo) {
    for (const choice of photos.children) choice.setAttribute('aria-pressed', String(choice === button));
    image.src = imagePath(photo.id);
    image.alt = '校园地点照片 · ' + formatDate.format(new Date(photo.created_at));
    viewer.hidden = false;
  }

  async function readPage(selection, offset) {
    more.disabled = true;
    status.textContent = '正在读取已发布照片…';
    try {
      const data = await request(selection.path + '?offset=' + offset, { signal: selection.signal });
      if (selection !== current || selection.signal.aborted) return;
      for (const photo of data.items) {
        if (selection.ids.has(photo.id)) continue;
        selection.ids.add(photo.id);
        const button = element('button', 'map-photo-thumb');
        button.type = 'button';
        button.setAttribute('aria-pressed', 'false');
        button.setAttribute('aria-label', '查看第 ' + selection.ids.size + ' 张照片');
        const thumbnail = element('img');
        thumbnail.alt = '';
        thumbnail.loading = 'lazy';
        thumbnail.src = imagePath(photo.id);
        button.append(thumbnail);
        button.addEventListener('click', () => showPhoto(button, photo));
        photos.append(button);
        if (viewer.hidden) showPhoto(button, photo);
      }
      status.textContent = selection.ids.size ? '点击缩略图查看该地点的照片。' : '这个地点暂时没有已发布的照片。';
      selection.next = data.next_offset;
      more.hidden = selection.next == null;
    } catch (error) {
      if (selection === current && !selection.signal.aborted) status.textContent = error.message;
    } finally {
      if (selection === current) more.disabled = false;
    }
  }

  more.addEventListener('click', () => void readPage(current, current.next));
  file.addEventListener('change', showDraft);
  draftImage.addEventListener('load', releaseDraftUrl);
  draftImage.addEventListener('error', () => {
    draftImage.hidden = true;
    releaseDraftUrl();
  });
  panelSignal.addEventListener('abort', clearDraft, { once: true });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const selection = current;
    const upload = file.files[0];
    if (!selection?.path || submit.disabled || !upload) return;
    if (!validSize(upload)) {
      feedback.textContent = `请选择一张不超过 ${maxUploadMb} MB 的照片。`;
      return;
    }
    uploadKey ??= crypto.randomUUID();
    file.disabled = submit.disabled = true;
    form.setAttribute('aria-busy', 'true');
    feedback.textContent = '正在提交照片…';
    try {
      const result = await request(selection.path, { method: 'POST', upload, key: uploadKey, signal: panelSignal });
      if (selection !== current || panelSignal.aborted) return;
      feedback.textContent = result.status === 'approved' ? '这张照片已发布。'
        : result.status === 'rejected' ? '这张照片未通过审核。' : '已提交审核。通过后会出现在这个地点的相册中。';
      form.reset();
      clearDraft();
      uploadKey = undefined;
    } catch (error) {
      if (selection === current && !panelSignal.aborted) feedback.textContent = error.message;
    } finally {
      if (selection === current) {
        file.disabled = false;
        submit.disabled = !file.files.length;
        form.removeAttribute('aria-busy');
      }
    }
  });

  function select(point) {
    current?.controller.abort();
    const controller = new AbortController();
    current = { controller, signal: AbortSignal.any([panelSignal, controller.signal]), ids: new Set() };
    heading.textContent = point.name + ' · 地点照片';
    viewer.hidden = more.hidden = true;
    image.removeAttribute('src');
    photos.replaceChildren();
    form.reset();
    clearDraft();
    uploadKey = undefined;
    file.disabled = false;
    submit.disabled = true;
    form.removeAttribute('aria-busy');
    feedback.textContent = '';
    const problem = unavailable || (point.eligible ? '' : '请在校区边界内选择拍摄点。');
    form.hidden = Boolean(problem);
    if (problem) {
      status.textContent = problem;
      return;
    }
    current.path = galleryPath + point.place + '/photos';
    void readPage(current, 0);
  }

  return { section, select };
}
