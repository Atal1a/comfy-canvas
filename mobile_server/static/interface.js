(() => {
  const q = selector => document.querySelector(selector);
  const qa = selector => [...document.querySelectorAll(selector)];

  document.addEventListener('click', event => {
    const link = event.target.closest('a[data-nav]');
    if (!link || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const historyCard = link.closest('#view-history .history-card');
    const historyPreviewTap = historyCard && link.matches('a.history-media') && window.innerWidth < 600
      && q('#view-history')?.classList.contains('canvas-history-mode-clean')
      && (!historyTouchLayoutEnabled() || historyMobileColumns === 1)
      && !historyCard.classList.contains('canvas-overlay-open');
    if (historyPreviewTap) return;
    document.documentElement.classList.remove('canvas-route-enter');
    requestAnimationFrame(() => document.documentElement.classList.add('canvas-route-enter'));
  }, true);

  document.addEventListener('click', event => {
    if (!matchMedia('(max-width:1099px)').matches) return;
    const link = event.target.closest('#detail-images .detail-card a');
    if (!link || event.target.closest('button,input,.compare-modes')) return;
    const result = link.closest('.result-detail');
    const results = [...document.querySelectorAll('#detail-images .result-detail')];
    const index = Math.max(0, results.indexOf(result));
    if (typeof state === 'undefined' || !state.detailJob) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    openDetailLightbox(state.detailJob, index);
  }, true);

  const lightbox = q('#detail-lightbox');
  const stage = q('#detail-lightbox-stage');
  const touches = new Map();
  let swipe = null;
  let pinch = null;
  const distance = () => {
    const points = [...touches.values()];
    return points.length > 1 ? Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y) : 0;
  };

  stage?.addEventListener('pointerdown', event => {
    if (event.pointerType !== 'touch' || event.target.closest('input,button')) return;
    touches.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (touches.size === 1) swipe = { id: event.pointerId, x: event.clientX, y: event.clientY, time: performance.now() };
    if (touches.size === 2 && typeof viewerZoom !== 'undefined') {
      swipe = null;
      pinch = { distance: distance(), scale: viewerZoom.detail.scale };
    }
  }, true);
  stage?.addEventListener('pointermove', event => {
    if (!touches.has(event.pointerId)) return;
    touches.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (!pinch || touches.size < 2 || typeof setViewerZoom === 'undefined') return;
    const current = distance();
    if (pinch.distance > 0) setViewerZoom('detail', pinch.scale * current / pinch.distance);
    event.preventDefault();
  }, { capture: true, passive: false });
  stage?.addEventListener('pointerup', event => {
    const wasPinching = Boolean(pinch);
    touches.delete(event.pointerId);
    if (touches.size < 2) pinch = null;
    if (wasPinching || !swipe || swipe.id !== event.pointerId) { swipe = null; return; }
    const dx = event.clientX - swipe.x;
    const dy = event.clientY - swipe.y;
    const elapsed = performance.now() - swipe.time;
    swipe = null;
    if (elapsed > 650 || Math.abs(dx) < 64 || Math.abs(dx) < Math.abs(dy) * 1.35) return;
    q(dx < 0 ? '#detail-lightbox-next' : '#detail-lightbox-prev')?.click();
  }, true);
  stage?.addEventListener('pointercancel', event => {
    touches.delete(event.pointerId);
    swipe = null;
    pinch = null;
  }, true);
  stage?.addEventListener('click', event => {
    if (event.target.closest('a')) event.preventDefault();
  }, true);
  lightbox?.addEventListener('close', () => {
    touches.clear();
    swipe = null;
    pinch = null;
  });

  const historyView = q('#view-history');
  const historyGallery = q('#gallery');
  const historyLoadMore = q('#load-more');
  const historyModeKey = 'nextHistoryMobileMode';
  const historyColumnsKey = 'nextHistoryMobileColumnsV1';
  let historyLayoutFrame = 0;
  let observedHistoryWidth = 0;
  let historyScrollTimer = 0;
  let historyPointer = null;
  let historyRenderObserver = null;
  let openHistoryCard = null;
  let historyPinch = null;
  let historySuppressClickUntil = 0;
  const historyTouches = new Map();
  let historyMobileMode = localStorage.getItem(historyModeKey) === 'params' ? 'params' : 'clean';
  const storedHistoryColumns = Number(localStorage.getItem(historyColumnsKey));
  let historyMobileColumns = Number.isInteger(storedHistoryColumns) ? Math.max(1, Math.min(3, storedHistoryColumns)) : 1;
  const historyTouchLayoutEnabled = () => window.innerWidth < 1100 && matchMedia('(hover: none) and (pointer: coarse)').matches;
  const historyExpanded = () => historyTouchLayoutEnabled() && historyMobileColumns > 1;
  const historyColumnCount = () => {
    const width = window.innerWidth;
    if (historyTouchLayoutEnabled()) return historyMobileColumns;
    if (width < 600) return 1;
    if (width < 920) return 2;
    if (width < 1400) return 3;
    if (width < 1800) return 4;
    return 5;
  };
  const resetHistoryCardFit = card => {
    card?.classList.remove('history-fit-ready', 'history-compact', 'history-minimal', 'history-actions-only', 'history-no-prompt');
    card?.style.removeProperty('--history-prompt-lines');
  };
  const closeHistoryOverlay = () => {
    openHistoryCard?.querySelector('a.history-media')?.removeAttribute('aria-expanded');
    openHistoryCard?.classList.remove('canvas-overlay-open');
    resetHistoryCardFit(openHistoryCard);
    openHistoryCard = null;
  };
  const syncHistoryDensityState = () => {
    const enabled = historyTouchLayoutEnabled();
    document.body.classList.toggle('canvas-history-touch', enabled);
    historyView?.classList.toggle('canvas-history-density-expanded', enabled && historyMobileColumns > 1);
    if (historyView) {
      if (enabled) historyView.dataset.historyColumns = String(historyMobileColumns);
      else delete historyView.dataset.historyColumns;
    }
  };
  const historyAnchorAt = (x = window.innerWidth / 2, y = window.innerHeight / 2) => {
    let card = document.elementFromPoint(x, y)?.closest('#gallery > .history-card');
    if (!card) {
      const cards = qa('#gallery > .history-card');
      card = cards.find(item => {
        const rect = item.getBoundingClientRect();
        return rect.bottom > 0 && rect.top < window.innerHeight;
      }) || null;
    }
    return card ? { card, top: card.getBoundingClientRect().top } : null;
  };
  const restoreHistoryAnchor = anchor => {
    if (!anchor?.card?.isConnected) return;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!anchor.card.isConnected || historyView?.hidden) return;
      const delta = anchor.card.getBoundingClientRect().top - anchor.top;
      if (Math.abs(delta) > .5) window.scrollBy(0, delta);
    }));
  };
  const setHistoryImageRatio = image => {
    const card = image.closest('.history-card');
    if (!card || !image.naturalWidth || !image.naturalHeight) return;
    card.style.setProperty('--history-ratio', `${image.naturalWidth} / ${image.naturalHeight}`);
    delete card.dataset.historyLayoutKey;
    delete card.dataset.historyHeight;
  };
  const fitHistoryCardOverlay = card => {
    const body = card.querySelector('.history-body');
    const prompt = body?.querySelector(':scope > p');
    const params = body?.querySelector('.history-params');
    const actions = body?.querySelector('.card-actions');
    const heading = body?.querySelector(':scope > div:first-child');
    if (!body || !prompt || !params || !actions || !heading) return;
    resetHistoryCardFit(card);
    if (historyExpanded()) return;
    card.classList.add('history-fit-ready');
    if (historyTouchLayoutEnabled() && historyMobileMode === 'params') {
      return;
    }
    const availablePromptHeight = () => {
      const style = getComputedStyle(body);
      const contentHeight = body.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
      const visibleFixed = [heading, params, actions].filter(item => getComputedStyle(item).display !== 'none');
      return contentHeight - visibleFixed.reduce((sum, item) => sum + item.offsetHeight, 0) - parseFloat(style.rowGap || style.gap || 0) * visibleFixed.length;
    };
    const lineHeight = parseFloat(getComputedStyle(prompt).lineHeight) || 18;
    const applyPromptFit = () => {
      card.classList.remove('history-no-prompt');
      card.style.removeProperty('--history-prompt-lines');
      const lines = Math.max(0, Math.floor(availablePromptHeight() / lineHeight));
      card.classList.toggle('history-no-prompt', lines < 1);
      if (lines > 0) card.style.setProperty('--history-prompt-lines', String(lines));
      return body.scrollHeight <= body.clientHeight + 1;
    };
    const canUseCompactSummary = window.innerWidth >= 600 || historyMobileMode === 'clean';
    if (applyPromptFit()) return;
    if (canUseCompactSummary) {
      card.classList.add('history-compact');
      if (applyPromptFit()) return;
    }
    if (canUseCompactSummary) {
      card.classList.add('history-minimal');
      if (applyPromptFit()) return;
    }
    card.classList.add('history-actions-only', 'history-no-prompt');
    card.style.removeProperty('--history-prompt-lines');
  };
  const historyGapForColumns = columns => historyTouchLayoutEnabled() ? (columns === 1 ? 12 : columns === 2 ? 8 : 6) : window.innerWidth < 600 ? 12 : 14;
  const historyCardRatio = card => {
    const raw = (card.style.getPropertyValue('--history-ratio') || getComputedStyle(card).getPropertyValue('--history-ratio')).trim();
    const match = raw.match(/^([\d.]+)\s*\/\s*([\d.]+)$/);
    if (match && Number(match[1]) > 0 && Number(match[2]) > 0) return Number(match[1]) / Number(match[2]);
    const media = card.querySelector('.history-media');
    const rect = media?.getBoundingClientRect();
    return rect?.width > 0 && rect?.height > 0 ? rect.width / rect.height : 4 / 3;
  };
  const calculateHistoryPreviewLayout = (cards, columns, galleryRect = historyGallery.getBoundingClientRect()) => {
    const gap = historyGapForColumns(columns);
    const width = (historyGallery.clientWidth - gap * (columns - 1)) / columns;
    const heights = Array(columns).fill(0);
    return cards.map((card, index) => {
      const lane = index % columns;
      const height = width / historyCardRatio(card);
      const rect = { x: galleryRect.left + lane * (width + gap), y: galleryRect.top + heights[lane], width, height };
      heights[lane] += height + gap;
      return rect;
    });
  };
  const layoutHistoryGallery = () => {
    historyLayoutFrame = 0;
    if (!historyGallery || historyView?.hidden || !historyGallery.clientWidth) return;
    syncHistoryDensityState();
    const cards = qa('#gallery > .history-card');
    historyGallery.classList.add('canvas-history-masonry');
    if (!cards.length) {
      historyGallery.style.height = '';
      return;
    }
    const columns = historyColumnCount();
    const gap = historyGapForColumns(columns);
    const width = (historyGallery.clientWidth - gap * (columns - 1)) / columns;
    const layoutKey = `${Math.round(width * 10) / 10}:${historyTouchLayoutEnabled() ? `${historyMobileMode}:${columns}` : window.innerWidth < 600 ? historyMobileMode : 'overlay'}`;
    const heights = Array(columns).fill(0);
    const placements = [];
    cards.forEach(card => {
      if (card.dataset.historyLayoutKey === layoutKey) return;
      card.dataset.historyLayoutKey = layoutKey;
      delete card.dataset.historyHeight;
      card.style.setProperty('--history-card-width', `${width}px`);
      resetHistoryCardFit(card);
      if (historyTouchLayoutEnabled() && historyMobileMode === 'params') card.classList.add('history-fit-ready');
      card.style.contentVisibility = 'visible';
    });
    cards.forEach((card, index) => {
      const lane = index % columns;
      const cachedHeight = Number(card.dataset.historyHeight);
      const cardHeight = cachedHeight > 0 ? cachedHeight : card.offsetHeight;
      if (!cachedHeight) card.dataset.historyHeight = String(cardHeight);
      placements.push([card, lane * (width + gap), heights[lane], index]);
      heights[lane] += cardHeight + gap;
    });
    placements.forEach(([card, left, top, index]) => {
      card.style.left = `${left}px`;
      card.style.top = `${top}px`;
      card.style.removeProperty('transform');
      card.style.removeProperty('content-visibility');
      card.dataset.historyOrder = String(index + 1);
      card.setAttribute('aria-posinset', String(index + 1));
      card.setAttribute('aria-setsize', String(cards.length));
      if (historyRenderObserver && !card.dataset.historyRenderObserved) {
        card.dataset.historyRenderObserved = 'true';
        historyRenderObserver.observe(card);
      }
    });
    if (!historyExpanded()) cards.filter(card => card.matches(':hover,:focus-within') || card.classList.contains('canvas-overlay-open')).forEach(fitHistoryCardOverlay);
    historyGallery.style.height = `${Math.max(...heights) - gap}px`;
  };
  const requestHistoryLayout = () => {
    if (historyPinch && !historyPinch.settling) {
      historyPinch.layoutPending = true;
      return;
    }
    if (historyLayoutFrame) cancelAnimationFrame(historyLayoutFrame);
    historyLayoutFrame = requestAnimationFrame(layoutHistoryGallery);
  };
  document.addEventListener('canvas-history-card-updated', event => {
    const card = event.detail?.card;
    if (!card?.matches?.('#gallery > .history-card') || historyView?.hidden) return;
    const anchor = { card, top: card.getBoundingClientRect().top };
    delete card.dataset.historyLayoutKey;
    delete card.dataset.historyHeight;
    requestHistoryLayout();
    restoreHistoryAnchor(anchor);
  });
  const applyHistoryColumns = (value, { persist = true, anchor = null, immediate = false } = {}) => {
    const columns = Math.max(1, Math.min(3, Math.round(Number(value) || 1)));
    if (columns === historyMobileColumns) {
      syncHistoryDensityState();
      return;
    }
    historyMobileColumns = columns;
    if (columns > 1 && historyMobileMode === 'params') {
      historyMobileMode = 'clean';
      localStorage.setItem(historyModeKey, historyMobileMode);
      historyView?.classList.add('canvas-history-mode-clean');
      historyView?.classList.remove('canvas-history-mode-params');
      qa('.mobile-history-mode button').forEach(button => {
        const active = button.dataset.historyMode === 'clean';
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
      });
    }
    if (persist) localStorage.setItem(historyColumnsKey, String(columns));
    closeHistoryOverlay();
    syncHistoryDensityState();
    qa('#gallery > .history-card').forEach(card => {
      delete card.dataset.historyLayoutKey;
      delete card.dataset.historyHeight;
    });
    if (immediate) {
      if (historyLayoutFrame) cancelAnimationFrame(historyLayoutFrame);
      historyLayoutFrame = 0;
      layoutHistoryGallery();
    } else {
      requestHistoryLayout();
      restoreHistoryAnchor(anchor);
    }
  };
  const applyHistoryMobileMode = (mode, persist = true) => {
    const nextMode = mode === 'params' ? 'params' : 'clean';
    if (nextMode === 'params' && historyTouchLayoutEnabled() && historyMobileColumns > 1) {
      applyHistoryColumns(1, { anchor: historyAnchorAt() });
    }
    historyMobileMode = nextMode;
    historyView?.classList.toggle('canvas-history-mode-clean', historyMobileMode === 'clean');
    historyView?.classList.toggle('canvas-history-mode-params', historyMobileMode === 'params');
    qa('.mobile-history-mode button').forEach(button => {
      const active = button.dataset.historyMode === historyMobileMode;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    if (persist) localStorage.setItem(historyModeKey, historyMobileMode);
    closeHistoryOverlay();
    requestAnimationFrame(requestHistoryLayout);
  };
  const installHistoryModeControl = () => {
    const filters = historyView?.querySelector('.filter-bar');
    if (!filters || q('.mobile-history-mode')) return;
    const control = document.createElement('div');
    control.className = 'mobile-history-mode';
    control.setAttribute('role', 'group');
    control.setAttribute('aria-label', '手机历史卡片显示模式');
    [['clean', '纯净版'], ['params', '带参数']].forEach(([mode, label]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.historyMode = mode;
      button.textContent = label;
      button.onclick = () => applyHistoryMobileMode(mode);
      control.append(button);
    });
    filters.insertAdjacentElement('afterend', control);
    applyHistoryMobileMode(historyMobileMode, false);
  };
  const prepareHistoryCards = () => {
    qa('#gallery > .history-card').forEach(card => {
      card.querySelector('.history-card-menu')?.remove();
      const image = card.querySelector('.history-media img');
      if (!image || image.dataset.nextMasonryBound) return;
      image.dataset.nextMasonryBound = 'true';
      if (image.complete) setHistoryImageRatio(image);
      image.addEventListener('load', () => {
        setHistoryImageRatio(image);
        requestHistoryLayout();
      });
      image.addEventListener('error', requestHistoryLayout, { once: true });
    });
    requestHistoryLayout();
  };
  const syncHistoryView = () => {
    const active = Boolean(historyView && !historyView.hidden);
    document.body.classList.toggle('canvas-history-active', active);
    syncHistoryDensityState();
    if (active) prepareHistoryCards();
    else closeHistoryOverlay();
  };
  const markHistoryScrolling = () => {
    if (historyView?.hidden) return;
    document.body.classList.add('history-scroll-active');
    clearTimeout(historyScrollTimer);
    historyScrollTimer = setTimeout(() => {
      document.body.classList.remove('history-scroll-active');
      if (!historyPointer || !matchMedia('(hover: hover)').matches) return;
      const card = document.elementFromPoint(historyPointer.x, historyPointer.y)?.closest('#gallery > .history-card');
      if (card) fitHistoryCardOverlay(card);
    }, 110);
  };

  if (historyGallery) {
    const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
    const mix = (from, to, progress) => from + (to - from) * progress;
    const smoothstep = value => value * value * (3 - 2 * value);
    const smootherstep = value => value * value * value * (value * (value * 6 - 15) + 10);
    const easeInOutCubic = value => value < .5 ? 4 * value * value * value : 1 - Math.pow(-2 * value + 2, 3) / 2;
    const pointsDistance = points => points.length > 1 ? Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y) : 0;
    const pointsMidpoint = points => points.length > 1 ? { x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2 } : null;
    const touchListPoints = touches => [...touches].slice(0, 2).map(touch => ({ x: touch.clientX, y: touch.clientY }));
    const rectIntersectsPinchBuffer = rect => rect.x + rect.width > -window.innerWidth * .25
      && rect.x < window.innerWidth * 1.25 && rect.y + rect.height > -window.innerHeight * .35
      && rect.y < window.innerHeight * 1.35;
    const transformPinchRect = (rect, origin, midpoint, scale) => ({
      x: midpoint.x + (rect.x - origin.x) * scale,
      y: midpoint.y + (rect.y - origin.y) * scale,
      width: rect.width * scale,
      height: rect.height * scale
    });
    const historyExitDistance = () => clamp(window.innerWidth * .0615, 20, 28);
    const historyLayerDistance = () => clamp(window.innerWidth * .215, 76, 96);
    const historyGestureState = (pinch, intent) => {
      const distanceDelta = pinch.startDistance - intent.distance;
      const direction = Math.sign(distanceDelta);
      const validDirection = direction > 0 ? pinch.startColumns < 3 : direction < 0 ? pinch.startColumns > 1 : false;
      let exitDistance = historyExitDistance(), layerDistance = historyLayerDistance();
      if (validDirection && direction > 0) {
        const availableDistance = pinch.startDistance - 16;
        if (availableDistance < 52) layerDistance = Infinity;
        else {
          const baseBoundary = exitDistance + layerDistance * .5;
          const reachableBoundary = Math.max(52, Math.min(baseBoundary, availableDistance * .9));
          const distanceScale = reachableBoundary / baseBoundary;
          exitDistance *= distanceScale;
          layerDistance *= distanceScale;
        }
      }
      const travel = Math.abs(distanceDelta);
      const exitProgress = validDirection ? clamp(travel / exitDistance, 0, 1) : 0;
      const densityTravel = validDirection && Number.isFinite(layerDistance)
        ? Math.max(0, travel - exitDistance) / layerDistance : 0;
      const rawColumns = clamp(pinch.startColumns + direction * densityTravel, 1, 3);
      const visualLayerDistance = Number.isFinite(layerDistance) ? layerDistance : historyLayerDistance();
      const imageFadeEnd = exitDistance + visualLayerDistance * .62;
      const imageFadeProgress = validDirection ? Math.pow(clamp(travel / imageFadeEnd, 0, 1), 2) : 0;
      const guideFadeStart = exitDistance * .2;
      const guideFadeEnd = exitDistance + visualLayerDistance * .3;
      return {
        direction, validDirection, travel, exitDistance, layerDistance, exitProgress, densityTravel, rawColumns,
        imageFadeProgress,
        guideProgress: validDirection ? smootherstep(clamp((travel - guideFadeStart) / (guideFadeEnd - guideFadeStart), 0, 1)) : 0
      };
    };
    const historyGuideWeights = (startColumns, displayColumns, motionOpacity, gestureDirection = 0) => {
      const weights = { 1: 0, 2: 0, 3: 0 };
      const delta = displayColumns - startColumns;
      const direction = Math.sign(delta) || gestureDirection;
      if (!direction || motionOpacity <= 0) return weights;
      const first = startColumns + direction;
      if (first < 1 || first > 3) return weights;
      const travel = Math.abs(delta), second = first + direction;
      if (travel <= 1 || second < 1 || second > 3) {
        weights[first] = motionOpacity;
        return weights;
      }
      const handoff = clamp(travel - 1, 0, 1);
      weights[first] = motionOpacity * (1 - handoff);
      weights[second] = motionOpacity * handoff;
      return weights;
    };
    const resistedHistoryScale = ratio => ratio < .5 ? .5 - (.5 - ratio) * .18 : ratio > 2.7 ? 2.7 + (ratio - 2.7) * .18 : ratio;
    const historyPinchIntent = pinch => {
      const distance = pointsDistance(pinch.lastPoints);
      const ratio = pinch.startDistance > 0 ? distance / pinch.startDistance : 1;
      return {
        distance, ratio,
        midpoint: pointsMidpoint(pinch.lastPoints) || pinch.startMidpoint
      };
    };
    const appendHistoryPinchTile = (snapshot, entryList, card, index, rect) => {
      if (entryList.some(entry => entry.index === index)) return null;
      const media = card.querySelector('.history-media');
      if (!media) return null;
      const tile = document.createElement('div'), clone = media.cloneNode(true);
      tile.className = 'history-pinch-tile';
      tile.style.left = `${rect.x}px`; tile.style.top = `${rect.y}px`;
      tile.style.width = `${rect.width}px`; tile.style.height = `${rect.height}px`;
      clone.removeAttribute('href'); clone.removeAttribute('data-nav'); clone.querySelector(':scope > span')?.remove();
      const image = clone.querySelector('img');
      if (image) {
        image.loading = 'eager';
        image.decoding = 'async';
      }
      tile.append(clone); snapshot.append(tile);
      const entry = { index, tile, rect };
      entryList.push(entry);
      return entry;
    };
    const ensureHistorySnapshotCoverage = (pinch, columns, transform, force = false) => {
      const destination = { x: transform.origin.x + transform.x, y: transform.origin.y + transform.y };
      const previous = pinch.coverage[columns];
      const moved = !previous || Math.hypot(destination.x - previous.x, destination.y - previous.y) > Math.min(window.innerWidth, window.innerHeight) * .25;
      const resized = !previous || Math.abs(Math.log(Math.max(.01, transform.scale) / Math.max(.01, previous.scale))) > .12;
      if (!force && !moved && !resized) return;
      pinch.coverage[columns] = { x: destination.x, y: destination.y, scale: transform.scale };
      pinch.cards.forEach((card, index) => {
        const rect = pinch.layouts[columns][index];
        const transformed = transformPinchRect(rect, transform.origin, destination, transform.scale);
        if (index !== pinch.anchorIndex && !rectIntersectsPinchBuffer(transformed)) return;
        appendHistoryPinchTile(pinch.snapshot, pinch.entries, card, index, rect);
      });
    };
    const historyTargetPlacement = (pinch, columns, midpoint) => {
      const targetRect = pinch.layouts[columns][pinch.anchorIndex];
      const targetOrigin = {
        x: targetRect.x + pinch.anchorPoint.x * targetRect.width,
        y: targetRect.y + pinch.anchorPoint.y * targetRect.height
      };
      const idealOffsetY = midpoint.y - targetOrigin.y;
      const galleryTop = historyGallery.getBoundingClientRect().top;
      const targetGalleryHeight = Math.max(...pinch.layouts[columns].map(rect => rect.y + rect.height)) - galleryTop;
      const projectedPageHeight = document.documentElement.scrollHeight - historyGallery.offsetHeight + targetGalleryHeight;
      const targetScrollY = clamp(window.scrollY - idealOffsetY, 0, Math.max(0, projectedPageHeight - window.innerHeight));
      return { targetRect, targetOrigin, targetScrollY, finalOffsetY: window.scrollY - targetScrollY };
    };
    const cachedHistorySourceRects = (cards, columns, galleryRect) => {
      let fallback = null;
      return cards.map((card, index) => {
        const left = Number.parseFloat(card.style.left), top = Number.parseFloat(card.style.top);
        const width = Number.parseFloat(card.style.getPropertyValue('--history-card-width'));
        const ratio = historyCardRatio(card);
        if ([left, top, width, ratio].every(Number.isFinite) && width > 0 && ratio > 0) {
          return { x: galleryRect.left + left, y: galleryRect.top + top, width, height: width / ratio };
        }
        fallback ||= calculateHistoryPreviewLayout(cards, columns, galleryRect);
        return fallback[index];
      });
    };
    const createHistoryColumnGuides = galleryRect => {
      const guides = document.createElement('div');
      guides.className = 'history-pinch-guides';
      guides.setAttribute('aria-hidden', 'true');
      [1, 2, 3].forEach(columns => {
        const set = document.createElement('div');
        set.className = 'history-pinch-guide-set';
        set.dataset.columns = String(columns);
        set.style.left = `${galleryRect.left}px`;
        set.style.width = `${galleryRect.width}px`;
        set.style.gap = `${historyGapForColumns(columns)}px`;
        set.style.setProperty('--history-guide-columns', String(columns));
        for (let index = 0; index < columns; index += 1) set.append(document.createElement('i'));
        guides.append(set);
      });
      return guides;
    };
    const syncHistoryColumnGuides = (pinch, displayColumns, motionOpacity, gestureDirection = 0) => {
      const weights = historyGuideWeights(pinch.startColumns, displayColumns, motionOpacity, gestureDirection);
      pinch.guides.querySelectorAll('.history-pinch-guide-set').forEach(set => {
        set.style.opacity = String(weights[Number(set.dataset.columns)] || 0);
      });
      return weights;
    };
    const createHistoryDensitySlider = () => {
      const slider = document.createElement('div'), rail = document.createElement('div');
      slider.className = 'history-pinch-density-slider';
      slider.setAttribute('aria-hidden', 'true');
      rail.className = 'history-pinch-density-rail';
      [1, 2, 3].forEach(columns => {
        const tick = document.createElement('i');
        tick.dataset.columns = String(columns);
        rail.append(tick);
      });
      [1.5, 2.5].forEach(value => {
        const boundary = document.createElement('span');
        boundary.className = 'history-pinch-density-boundary';
        boundary.style.left = `${(value - 1) / 2 * 100}%`;
        rail.append(boundary);
      });
      const thumb = document.createElement('b');
      thumb.className = 'history-pinch-density-thumb';
      rail.append(thumb);
      slider.append(rail);
      return slider;
    };
    const syncHistoryDensitySlider = (pinch, displayColumns, settlingTarget = null, gestureState = null) => {
      const value = settlingTarget === null ? displayColumns : settlingTarget;
      const nearest = Math.round(value);
      pinch.slider.style.setProperty('--history-density-progress', `${clamp((value - 1) / 2, 0, 1) * 100}%`);
      pinch.slider.style.setProperty('--history-exit-progress', String(gestureState?.exitProgress || 0));
      pinch.slider.classList.toggle('is-changing', nearest !== pinch.startColumns);
      pinch.slider.classList.toggle('is-preparing', settlingTarget === null && Boolean(gestureState?.validDirection) && gestureState.exitProgress > 0 && gestureState.densityTravel <= .001);
      pinch.slider.classList.toggle('is-settling', settlingTarget !== null);
      pinch.slider.dataset.targetColumns = String(nearest);
      pinch.slider.querySelectorAll('.history-pinch-density-rail > i').forEach(tick => {
        tick.classList.toggle('is-target', Number(tick.dataset.columns) === nearest);
        tick.classList.toggle('is-source', Number(tick.dataset.columns) === pinch.startColumns);
      });
    };
    const scheduleHistoryPinchRender = pinch => {
      if (pinch.frame || pinch.settling) return;
      pinch.frame = requestAnimationFrame(() => {
        pinch.frame = 0;
        renderHistoryPinch(pinch);
      });
    };
    const renderHistoryPinch = pinch => {
      if (historyPinch !== pinch) return;
      const intent = historyPinchIntent(pinch);
      const midpoint = intent.midpoint;
      const translation = { x: midpoint.x - pinch.startMidpoint.x, y: midpoint.y - pinch.startMidpoint.y };
      const liveScale = resistedHistoryScale(intent.ratio);
      const sourceColumns = pinch.startColumns;
      const gestureState = historyGestureState(pinch, intent);
      const { rawColumns, imageFadeProgress } = gestureState;
      const displayColumns = rawColumns;
      const sourceSnapshot = pinch.snapshot;
      const sourceTransform = { x: translation.x, y: translation.y, scale: liveScale, origin: pinch.startMidpoint };
      if (imageFadeProgress < .98) ensureHistorySnapshotCoverage(pinch, sourceColumns, sourceTransform);
      sourceSnapshot.style.zIndex = '2';
      sourceSnapshot.style.opacity = '1';
      sourceSnapshot.style.transformOrigin = `${pinch.startMidpoint.x}px ${pinch.startMidpoint.y}px`;
      sourceSnapshot.style.transform = `translate3d(${translation.x}px,${translation.y}px,0) scale(${liveScale})`;
      pinch.entries.forEach(entry => {
        entry.tile.style.transform = 'none';
        entry.tile.style.opacity = String(1 - imageFadeProgress);
      });
      const guideWeights = syncHistoryColumnGuides(pinch, rawColumns, gestureState.guideProgress, gestureState.direction);
      pinch.layer.style.setProperty('--history-pinch-x', `${midpoint.x}px`);
      pinch.layer.style.setProperty('--history-pinch-y', `${midpoint.y}px`);
      pinch.layer.style.setProperty('--history-pinch-backdrop-opacity', String(.86 * imageFadeProgress));
      syncHistoryDensitySlider(pinch, displayColumns, null, gestureState);
      pinch.layer.style.opacity = '1';
      historyGallery.style.setProperty('--history-pinch-crossfade', '1');
      pinch.visual = {
        intent, midpoint, translation, liveScale, sourceTransform, gestureState,
        rawColumns, displayColumns, selectedColumns: Math.round(displayColumns), imageFadeProgress, guideWeights
      };
    };
    const createHistoryPinchLayer = (points, source) => {
      if (!historyTouchLayoutEnabled() || historyPinch || points.length < 2) return false;
      const startDistance = pointsDistance(points), midpoint = pointsMidpoint(points);
      if (startDistance < 20 || !midpoint) return false;
      closeHistoryOverlay();
      const cards = qa('#gallery > .history-card');
      if (!cards.length) return false;
      const galleryRect = historyGallery.getBoundingClientRect();
      const layouts = [null], sourceRects = cachedHistorySourceRects(cards, historyMobileColumns, galleryRect);
      layouts[historyMobileColumns] = sourceRects;
      let anchorIndex = cards.findIndex(card => card === document.elementFromPoint(midpoint.x, midpoint.y)?.closest('#gallery > .history-card'));
      if (anchorIndex < 0) anchorIndex = sourceRects.reduce((best, rect, index) => {
        const distance = Math.hypot(rect.x + rect.width / 2 - midpoint.x, rect.y + rect.height / 2 - midpoint.y);
        return distance < best.distance ? { index, distance } : best;
      }, { index: 0, distance: Infinity }).index;
      const measuredAnchorRect = cards[anchorIndex].querySelector('.history-media')?.getBoundingClientRect();
      if (measuredAnchorRect?.width > 0 && measuredAnchorRect?.height > 0) {
        sourceRects[anchorIndex] = { x: measuredAnchorRect.left, y: measuredAnchorRect.top, width: measuredAnchorRect.width, height: measuredAnchorRect.height };
      }
      const sourceAnchorRect = sourceRects[anchorIndex];
      const anchorPoint = {
        x: clamp((midpoint.x - sourceAnchorRect.x) / sourceAnchorRect.width, 0, 1),
        y: clamp((midpoint.y - sourceAnchorRect.y) / sourceAnchorRect.height, 0, 1)
      };
      const layer = document.createElement('div');
      layer.className = 'history-pinch-layer';
      layer.setAttribute('aria-hidden', 'true');
      const backdrop = document.createElement('div');
      backdrop.className = 'history-pinch-backdrop';
      const guides = createHistoryColumnGuides(galleryRect);
      layer.append(backdrop, guides);
      const snapshot = document.createElement('div'), entries = [];
      snapshot.className = 'history-pinch-snapshot';
      snapshot.dataset.columns = String(historyMobileColumns);
      snapshot.hidden = false;
      layer.append(snapshot);
      const slider = createHistoryDensitySlider();
      layer.append(slider);
      document.body.append(layer);
      historyPinch = {
        source, cards, layouts, sourceRects, layer, backdrop, guides, snapshot, entries, slider, anchorIndex, startDistance,
        startMidpoint: midpoint, lastPoints: points, startColumns: historyMobileColumns,
        anchorPoint, coverage: {}, frame: 0, layoutPending: false, moved: false
      };
      historyGallery.classList.add('history-pinch-live');
      historyGallery.style.setProperty('--history-pinch-crossfade', '1');
      layer.style.opacity = '1';
      syncHistoryDensitySlider(historyPinch, historyMobileColumns);
      renderHistoryPinch(historyPinch);
      return true;
    };
    const updateHistoryPinch = points => {
      const pinch = historyPinch;
      if (!pinch || points.length < 2) return;
      pinch.lastPoints = points;
      if (!pinch.moved) {
        pinch.moved = true;
        if (pinch.frame) { cancelAnimationFrame(pinch.frame); pinch.frame = 0; }
        renderHistoryPinch(pinch);
      } else scheduleHistoryPinchRender(pinch);
    };
    const cleanupHistoryPinch = pinch => {
      if (pinch.frame) cancelAnimationFrame(pinch.frame);
      pinch.outlinedCards?.forEach(card => {
        card.classList.remove('history-pinch-confirmed');
        card.style.removeProperty('--history-pinch-outline-local-opacity');
        card.style.removeProperty('--history-pinch-outline-brightness');
      });
      pinch.layer.remove();
      historyGallery.classList.remove('history-pinch-live', 'history-pinch-settling', 'history-pinch-committing');
      historyGallery.style.removeProperty('--history-pinch-crossfade');
      historyGallery.style.removeProperty('--history-pinch-outline-opacity');
      historyPinch = null;
      if (pinch.layoutPending) requestHistoryLayout();
    };
    const abortHistoryPinch = () => {
      const pinch = historyPinch;
      if (!pinch) return;
      pinch.layoutPending = true;
      cleanupHistoryPinch(pinch);
    };
    const finishHistoryPinch = commit => {
      const pinch = historyPinch;
      if (!pinch || pinch.settling) return;
      historySuppressClickUntil = performance.now() + 450;
      if (pinch.frame) { cancelAnimationFrame(pinch.frame); pinch.frame = 0; }
      renderHistoryPinch(pinch);
      if (pinch.frame) { cancelAnimationFrame(pinch.frame); pinch.frame = 0; }
      const target = commit ? pinch.visual.selectedColumns : pinch.startColumns;
      const canCommit = target !== pinch.startColumns;
      const visual = pinch.visual;
      if (canCommit) pinch.layouts[target] = calculateHistoryPreviewLayout(pinch.cards, target, historyGallery.getBoundingClientRect());
      const placement = canCommit ? historyTargetPlacement(pinch, target, visual.midpoint) : null;
      const targetScrollY = placement?.targetScrollY ?? window.scrollY;
      const sourceScrollY = window.scrollY;
      const fromTransform = visual.sourceTransform;
      pinch.settling = true;
      pinch.layer.style.opacity = '1';
      historyGallery.style.setProperty('--history-pinch-crossfade', '1');
      if (canCommit) {
        applyHistoryColumns(target, { immediate: true });
        if (Math.abs(window.scrollY - targetScrollY) > .5) window.scrollTo(0, targetScrollY);
      }
      syncHistoryDensitySlider(pinch, visual.displayColumns, canCommit ? target : pinch.startColumns);
      const sourceOpacity = new Map(pinch.entries.map(entry => {
        const opacity = Number(entry.tile.style.opacity);
        return [entry, Number.isFinite(opacity) ? opacity : 1];
      }));
      const guideOpacity = new Map([...pinch.guides.querySelectorAll('.history-pinch-guide-set')].map(set => [set, Number(set.style.opacity || 0)]));
      const targetGuide = pinch.guides.querySelector(`.history-pinch-guide-set[data-columns="${target}"]`);
      const backdropOpacity = .86 * visual.imageFadeProgress;
      const started = performance.now(), duration = matchMedia('(prefers-reduced-motion: reduce)').matches ? 100 : canCommit ? 420 : 320;
      historyGallery.classList.add('history-pinch-settling');
      const settle = now => {
        if (historyPinch !== pinch) return;
        const progress = clamp((now - started) / duration, 0, 1), eased = 1 - Math.pow(1 - progress, 3);
        if (canCommit) {
          const backgroundProgress = smootherstep(clamp((progress - .06) / .94, 0, 1));
          pinch.snapshot.style.opacity = String(1 - backgroundProgress);
          guideOpacity.forEach((opacity, set) => {
            const guideHandoffOpacity = set === targetGuide ? .3 : 0;
            set.style.opacity = String(mix(opacity, guideHandoffOpacity, backgroundProgress));
          });
          pinch.backdrop.style.opacity = String(backdropOpacity * (1 - backgroundProgress));
          pinch.slider.style.opacity = String(1 - smootherstep(clamp((progress - .62) / .38, 0, 1)));
          historyGallery.style.setProperty('--history-pinch-crossfade', String(1 - backgroundProgress));
        } else {
          const galleryProgress = smootherstep(clamp((progress - .06) / .94, 0, 1));
          pinch.snapshot.style.transformOrigin = `${fromTransform.origin.x}px ${fromTransform.origin.y}px`;
          pinch.snapshot.style.transform = `translate3d(${mix(fromTransform.x, 0, eased)}px,${mix(fromTransform.y, 0, eased)}px,0) scale(${mix(fromTransform.scale, 1, eased)})`;
          sourceOpacity.forEach((opacity, entry) => { entry.tile.style.opacity = String(mix(opacity, 1, eased)); });
          pinch.snapshot.style.opacity = String(1 - galleryProgress);
          guideOpacity.forEach((opacity, set) => { set.style.opacity = String(opacity * (1 - eased)); });
          pinch.backdrop.style.opacity = String(backdropOpacity * (1 - eased));
          pinch.slider.style.opacity = String(1 - eased);
          historyGallery.style.setProperty('--history-pinch-crossfade', String(1 - galleryProgress));
        }
        if (progress < 1) { pinch.frame = requestAnimationFrame(settle); return; }
        pinch.frame = 0;
        historyGallery.classList.add('history-pinch-committing');
        historyGallery.style.setProperty('--history-pinch-crossfade', '0');
        if (canCommit && targetGuide) {
          pinch.snapshot.style.opacity = '0';
          pinch.backdrop.style.opacity = '0';
          pinch.slider.style.opacity = '0';
          pinch.guides.querySelectorAll('.history-pinch-guide-set').forEach(set => {
            if (set !== targetGuide) set.style.opacity = '0';
          });
          targetGuide.style.opacity = '.3';
          const scrollDelta = sourceScrollY - targetScrollY;
          const outlineCandidates = pinch.cards.map((card, index) => {
            if (!card.querySelector('.history-media')) return null;
            const rect = pinch.layouts[target][index];
            const visibleRect = { ...rect, y: rect.y + scrollDelta };
            if (visibleRect.y + visibleRect.height <= 0 || visibleRect.y >= window.innerHeight) return null;
            return {
              card,
              distance: Math.hypot(
                visibleRect.x + visibleRect.width / 2 - visual.midpoint.x,
                visibleRect.y + visibleRect.height / 2 - visual.midpoint.y
              )
            };
          }).filter(Boolean);
          const outlineDistances = outlineCandidates.map(entry => entry.distance);
          const minOutlineDistance = Math.min(...outlineDistances);
          const maxOutlineDistance = Math.max(...outlineDistances);
          const outlineDistanceRange = maxOutlineDistance - minOutlineDistance;
          const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
          const outlinePropagation = reduceMotion || outlineCandidates.length < 2 ? 0 : 180;
          const outlineRise = reduceMotion ? 0 : 90;
          const outlineHold = reduceMotion ? 0 : 100;
          const outlineFade = reduceMotion ? 120 : 400;
          const outlineMovesOutward = target > pinch.startColumns;
          pinch.outlineEntries = outlineCandidates.map(entry => {
            const distanceProgress = outlineDistanceRange > .5
              ? (entry.distance - minOutlineDistance) / outlineDistanceRange : 0;
            const waveProgress = outlineMovesOutward ? distanceProgress : 1 - distanceProgress;
            return { ...entry, delay: outlinePropagation * waveProgress };
          });
          pinch.outlinedCards = pinch.outlineEntries.map(entry => entry.card);
          pinch.outlinedCards.forEach(card => {
            card.classList.add('history-pinch-confirmed');
            card.style.setProperty('--history-pinch-outline-local-opacity', reduceMotion ? '1' : '0');
            card.style.setProperty('--history-pinch-outline-brightness', '1');
          });
          historyGallery.style.removeProperty('--history-pinch-outline-opacity');
          const confirmationStarted = performance.now();
          const outlineGuideHandoff = reduceMotion ? 60 : 180;
          const outlineAllLitAt = outlinePropagation + outlineRise;
          const animateConfirmation = confirmationNow => {
            if (historyPinch !== pinch) return;
            const elapsed = confirmationNow - confirmationStarted;
            const guideProgress = clamp(elapsed / outlineGuideHandoff, 0, 1);
            const fadeProgress = clamp((elapsed - outlineAllLitAt - outlineHold) / outlineFade, 0, 1);
            targetGuide.style.opacity = String(.3 * (1 - smootherstep(guideProgress)));
            if (elapsed < outlineAllLitAt) {
              pinch.outlineEntries.forEach(entry => {
                const riseProgress = outlineRise > 0 ? clamp((elapsed - entry.delay) / outlineRise, 0, 1) : 1;
                const easedRise = smootherstep(riseProgress);
                entry.card.style.setProperty('--history-pinch-outline-local-opacity', String(easedRise));
                entry.card.style.setProperty('--history-pinch-outline-brightness', String(1 + .16 * Math.sin(Math.PI * riseProgress)));
              });
            } else {
              if (!pinch.outlinePropagationComplete) {
                pinch.outlinePropagationComplete = true;
                pinch.outlineEntries.forEach(entry => {
                  entry.card.style.setProperty('--history-pinch-outline-local-opacity', '1');
                  entry.card.style.setProperty('--history-pinch-outline-brightness', '1');
                });
              }
              historyGallery.style.setProperty('--history-pinch-outline-opacity', String(1 - easeInOutCubic(fadeProgress)));
            }
            if (fadeProgress < 1) { pinch.frame = requestAnimationFrame(animateConfirmation); return; }
            pinch.frame = 0;
            pinch.layer.style.opacity = '0';
            cleanupHistoryPinch(pinch);
          };
          pinch.frame = requestAnimationFrame(animateConfirmation);
          return;
        }
        pinch.layer.classList.add('history-pinch-layer-committing');
        pinch.layer.style.opacity = '0';
        setTimeout(() => cleanupHistoryPinch(pinch), 80);
      };
      pinch.frame = requestAnimationFrame(settle);
    };
    const supportsNativeTouch = 'ontouchstart' in window;
    historyGallery.addEventListener('touchstart', event => {
      if (!historyTouchLayoutEnabled() || event.target.closest('button,input,select')) return;
      const points = touchListPoints(event.touches);
      if (points.length < 2) return;
      event.preventDefault();
      if (!historyPinch) createHistoryPinchLayer(points, 'touch');
    }, { capture: true, passive: false });
    historyGallery.addEventListener('touchmove', event => {
      if (!historyPinch || historyPinch.source !== 'touch') return;
      const points = touchListPoints(event.touches);
      if (points.length > 1) { event.preventDefault(); updateHistoryPinch(points); }
    }, { capture: true, passive: false });
    historyGallery.addEventListener('touchend', event => {
      if (historyPinch?.source === 'touch' && event.touches.length < 2) finishHistoryPinch(true);
    }, true);
    historyGallery.addEventListener('touchcancel', event => {
      if (historyPinch?.source === 'touch' && event.touches.length < 2) finishHistoryPinch(false);
    }, true);
    if (!supportsNativeTouch) {
      const pointerPoints = () => [...historyTouches.values()].slice(0, 2);
      historyGallery.addEventListener('pointerdown', event => {
        if (!historyTouchLayoutEnabled() || event.pointerType !== 'touch' || event.target.closest('button,input,select')) return;
        historyTouches.set(event.pointerId, { x: event.clientX, y: event.clientY });
        const points = pointerPoints();
        if (points.length > 1) {
          event.preventDefault();
          if (!historyPinch) createHistoryPinchLayer(points, 'pointer');
        }
      }, { passive: false });
      historyGallery.addEventListener('pointermove', event => {
        if (!historyTouches.has(event.pointerId)) return;
        historyTouches.set(event.pointerId, { x: event.clientX, y: event.clientY });
        if (historyPinch?.source === 'pointer') { event.preventDefault(); updateHistoryPinch(pointerPoints()); }
      }, { passive: false });
      historyGallery.addEventListener('pointerup', event => {
        historyTouches.delete(event.pointerId);
        if (historyPinch?.source === 'pointer' && historyTouches.size < 2) finishHistoryPinch(true);
      }, true);
      historyGallery.addEventListener('pointercancel', event => {
        historyTouches.delete(event.pointerId);
        if (historyPinch?.source === 'pointer' && historyTouches.size < 2) finishHistoryPinch(false);
      }, true);
    }
    ['gesturestart', 'gesturechange', 'gestureend'].forEach(type => document.addEventListener(type, event => {
      if (!historyPinch && !event.target.closest?.('#gallery')) return;
      event.preventDefault();
    }, { capture: true, passive: false }));
    window.addEventListener('resize', abortHistoryPinch, { passive: true });
    window.addEventListener('pagehide', abortHistoryPinch, { passive: true });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) abortHistoryPinch();
    });
    historyGallery.addEventListener('click', event => {
      if (performance.now() >= historySuppressClickUntil) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    }, true);
    if ('IntersectionObserver' in window) historyRenderObserver = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        const card = entry.target;
        card.classList.toggle('history-nearby', entry.isIntersecting);
        if (!entry.isIntersecting) return;
        const image = card.querySelector('.history-media img');
        if (!image) return;
        image.loading = 'eager';
        image.decode?.().catch(() => {});
      });
    }, { rootMargin: '1600px 0px' });
    new MutationObserver(prepareHistoryCards).observe(historyGallery, { childList: true });
    historyGallery.addEventListener('pointerover', event => {
      if (!matchMedia('(hover: hover)').matches || document.body.classList.contains('history-scroll-active')) return;
      const card = event.target.closest('.history-card');
      if (!card || card.contains(event.relatedTarget)) return;
      fitHistoryCardOverlay(card);
    });
    historyGallery.addEventListener('pointermove', event => {
      if (event.pointerType === 'mouse') historyPointer = { x: event.clientX, y: event.clientY };
    }, { passive: true });
    historyGallery.addEventListener('focusin', event => {
      const card = event.target.closest('.history-card');
      if (card) fitHistoryCardOverlay(card);
    });
    if ('ResizeObserver' in window) new ResizeObserver(entries => {
      const width = entries[0]?.contentRect.width || 0;
      if (Math.abs(width - observedHistoryWidth) < .5) return;
      observedHistoryWidth = width;
      requestHistoryLayout();
    }).observe(historyGallery);
  }
  document.fonts?.ready.then(() => {
    qa('#gallery > .history-card').forEach(card => {
      delete card.dataset.historyLayoutKey;
      delete card.dataset.historyHeight;
    });
    requestHistoryLayout();
  });
  if (historyLoadMore && 'IntersectionObserver' in window) {
    new IntersectionObserver(entries => {
      const entry = entries[0];
      if (!entry?.isIntersecting || historyView?.hidden || historyLoadMore.hidden) return;
      historyLoadMore.textContent = '正在自动加载…';
      Promise.resolve(loadHistory()).finally(() => {
        historyLoadMore.textContent = '加载更多';
        requestHistoryLayout();
      });
    }, { rootMargin: '500px 0px' }).observe(historyLoadMore);
  }
  if (historyView) new MutationObserver(syncHistoryView).observe(historyView, { attributes: true, attributeFilter: ['hidden'] });
  window.addEventListener('resize', requestHistoryLayout, { passive: true });
  window.addEventListener('wheel', markHistoryScrolling, { passive: true });
  window.addEventListener('scroll', () => {
    if (openHistoryCard) closeHistoryOverlay();
    markHistoryScrolling();
  }, { passive: true });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeHistoryOverlay();
  });
  document.addEventListener('click', event => {
    const card = event.target.closest('#view-history .history-card');
    if (!card) {
      closeHistoryOverlay();
      return;
    }
    const media = event.target.closest('a.history-media');
    const cleanMobileTap = (historyTouchLayoutEnabled() ? historyMobileColumns === 1 : window.innerWidth < 600) && historyMobileMode === 'clean' && media;
    if (cleanMobileTap && openHistoryCard !== card) {
      event.preventDefault();
      event.stopImmediatePropagation();
      closeHistoryOverlay();
      openHistoryCard = card;
      card.classList.add('canvas-overlay-open');
      media.setAttribute('aria-expanded', 'true');
      fitHistoryCardOverlay(card);
      return;
    }
    if (media) closeHistoryOverlay();
  }, true);
  installHistoryModeControl();
  syncHistoryView();
})();
