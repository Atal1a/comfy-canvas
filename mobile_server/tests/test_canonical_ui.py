from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import mobile_server.app as app_module
from PIL import Image
from starlette.requests import Request
from starlette.responses import Response


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
RUNTIME_STATIC = ROOT / "static_dist"


class CanonicalUiTests(unittest.TestCase):
    def test_all_pages_use_the_shared_brand_and_web_app_metadata(self) -> None:
        pages = (STATIC / "index.html", STATIC / "auth.html")
        for page in pages:
            with self.subTest(page=page.name):
                html = page.read_text(encoding="utf-8")
                self.assertIn('<link rel="icon" sizes="32x32" href="/static/icons/fraunces-favicon-v2-32.png" type="image/png">', html)
                self.assertIn('<link rel="icon" sizes="64x64" href="/static/icons/fraunces-favicon-v2-64.png" type="image/png">', html)
                self.assertIn('<link rel="icon" sizes="192x192" href="/static/icons/fraunces-app-v1-192.png" type="image/png">', html)
                self.assertIn('<link rel="shortcut icon" href="/static/icons/fraunces-favicon-v2-64.png" type="image/png">', html)
                self.assertIn('<link rel="manifest" href="/static/manifest-fraunces-v1.webmanifest">', html)
                self.assertIn('<link rel="apple-touch-icon" sizes="180x180" href="/static/icons/fraunces-touch-v1-180.png">', html)
                self.assertNotIn("apple-touch-icon-precomposed", html)
                self.assertIn('<meta name="apple-mobile-web-app-capable" content="yes">', html)
                self.assertIn('<meta name="apple-mobile-web-app-status-bar-style" content="black">', html)
                self.assertIn('<meta name="apple-mobile-web-app-title" content="Comfy Canvas">', html)
        icon = (STATIC / "favicon.png").read_bytes()
        self.assertTrue(icon.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_web_app_manifest_and_icons_are_installable(self) -> None:
        manifest = json.loads((STATIC / "manifest-fraunces-v1.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "/comfy-canvas-fraunces-v1")
        self.assertEqual(manifest["name"], "Comfy Canvas")
        self.assertEqual(manifest["short_name"], "Comfy Canvas")
        self.assertEqual(manifest["start_url"], "/?source=fraunces-web-app-v1")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["theme_color"], "#070914")
        self.assertEqual(manifest["background_color"], "#070914")
        self.assertEqual(
            {(icon["sizes"], icon["purpose"]) for icon in manifest["icons"]},
            {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")},
        )
        expected_sizes = {
            "favicon.png": (64, 64),
            "icons/fraunces-favicon-v2-32.png": (32, 32),
            "icons/fraunces-favicon-v2-64.png": (64, 64),
            "icons/apple-touch-icon.png": (180, 180),
            "icons/icon-192.png": (192, 192),
            "icons/icon-512.png": (512, 512),
            "icons/icon-maskable-512.png": (512, 512),
            "icons/fraunces-touch-v1-180.png": (180, 180),
            "icons/fraunces-app-v1-192.png": (192, 192),
            "icons/fraunces-app-v1-512.png": (512, 512),
            "icons/fraunces-maskable-v1-512.png": (512, 512),
        }
        for relative, size in expected_sizes.items():
            with self.subTest(icon=relative), Image.open(STATIC / relative) as image:
                self.assertEqual(image.size, size)
                self.assertEqual(image.mode, "RGB")

    def test_apple_touch_icon_has_public_root_fallbacks(self) -> None:
        source = Path(app_module.__file__).read_text(encoding="utf-8")
        for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/apple-touch-icon-180x180.png"):
            with self.subTest(path=path):
                self.assertIn(f'"{path}"', source)
        response = asyncio.run(app_module.apple_touch_icon())
        self.assertEqual(Path(response.path), RUNTIME_STATIC / "icons" / "apple-touch-icon.png")
        self.assertEqual(response.media_type, "image/png")

    def test_favicon_has_public_root_fallbacks(self) -> None:
        source = Path(app_module.__file__).read_text(encoding="utf-8")
        for path in ("/favicon.ico", "/favicon.png"):
            with self.subTest(path=path):
                self.assertIn(f'"{path}"', source)
        png_response = asyncio.run(app_module.favicon_png())
        ico_response = asyncio.run(app_module.favicon_ico())
        self.assertEqual(Path(png_response.path), RUNTIME_STATIC / "icons" / "fraunces-favicon-v2-64.png")
        self.assertEqual(png_response.media_type, "image/png")
        self.assertEqual(Path(ico_response.path), RUNTIME_STATIC / "icons" / "fraunces-favicon-v2.ico")
        self.assertEqual(ico_response.media_type, "image/x-icon")

    def test_visible_brand_marks_use_the_shared_svg(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        auth = (STATIC / "auth.html").read_text(encoding="utf-8")
        svg = (STATIC / "brand-icon.svg").read_text(encoding="utf-8")
        self.assertEqual(html.count('class="brand-mark" src="/static/brand-icon.svg"'), 2)
        self.assertEqual(auth.count('class="brand-mark" src="/static/brand-icon.svg"'), 1)
        self.assertNotIn('<span class="brand-mark">', html + auth)
        self.assertEqual(svg.count("<path "), 1)
        self.assertIn("bold italic Fraunces letter C", svg)

    def test_canonical_bundle_uses_only_canonical_assets(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('/static/style.css?v=109', html)
        self.assertIn('/static/interface.css?v=62', html)
        self.assertIn('/static/icons.js?v=4', html)
        self.assertIn('/static/chat.js?v=28', html)
        self.assertIn('/static/app.js?v=60', html)
        self.assertIn('/static/interface.js?v=21', html)
        self.assertNotIn('/static-next/', html)

    def test_canonical_bundle_is_fully_isolated(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        auth = (STATIC / "auth.html").read_text(encoding="utf-8")
        self.assertIn('class="canvas-ui"', html)
        self.assertIn('class="canvas-ui auth-body"', auth)
        self.assertIn('/static/style.css?v=109', html)
        self.assertIn('/static/interface.css?v=62', html)
        self.assertIn('/static/app.js?v=60', html)
        self.assertIn('/static/interface.js?v=21', html)
        self.assertIn('/static/style.css?v=109', auth)
        self.assertIn('/static/interface.css?v=60', auth)
        self.assertIn('/static/auth.js?v=1', auth)
        self.assertNotIn('static-next', html)

    def test_history_detail_updates_result_cards_incrementally(self) -> None:
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("function detailItemRenderSignature", script)
        self.assertIn("function reconcileDetailResults", script)
        self.assertIn("node.dataset.itemId=String(item.id)", script)
        self.assertIn("if(node)node.replaceWith(replacement)", script)
        self.assertIn("if(position!==node)root.insertBefore(node,position||null)", script)
        self.assertNotIn("正在同步最新状态…", script)

    def test_create_labels_seeds_originals_and_history_stay_lossless_and_live(self) -> None:
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("function loraOptionLabel(name='')", script)
        self.assertIn("new Option(loraOptionLabel(name),name", script)
        self.assertIn("const SEED_FIELDS=new Set(['seed','stage1_seed','stage2_seed'])", script)
        self.assertIn("input.type='text';input.inputMode='numeric'", script)
        self.assertIn("function originalImageTargets(root,src,label)", script)
        self.assertIn("Promise.all(pending.map", script)
        self.assertIn("function scheduleHistorySync(extra=1)", script)
        self.assertIn("function completedOwnGenerationCount(previous,next)", script)
        self.assertIn("applyTaskEventSnapshot(snapshot)", script)
        self.assertIn("loadActivity().finally(()=>scheduleHistorySync(0))", script)

    def test_canonical_client_scopes_routes_and_login(self) -> None:
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        auth = (STATIC / "auth.js").read_text(encoding="utf-8")
        self.assertIn("function appPath(){return location.pathname||'/'}", script)
        self.assertIn("function appUrl(url){const target=new URL(url,location.origin);return target.pathname+target.search+target.hash}", script)
        self.assertIn("const path=appPath(),key=viewForPath(path)", script)
        self.assertIn("const detailDesktopMedia=window.matchMedia('(min-width:1100px)')", script)
        self.assertIn("function desktopDetailMode(){return detailDesktopMedia.matches}", script)
        self.assertIn("detailDesktopMedia.addEventListener('change'", script)
        self.assertIn("function loraMetaMobile(){return matchMedia('(max-width:760px)').matches}", script)
        self.assertIn("location.replace('/login')", script)
        self.assertIn("location.pathname==='/register'", auth)
        self.assertIn("?next=${encodeURIComponent(location.pathname+location.search)}", script)

    def test_rerun_restore_returns_to_prompt_step(self) -> None:
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("loadRerun=async function(id){await baseLoadRerun(id)", script)
        self.assertIn("if(state.loadedRerunId!==id)return;showCreateStep(1,false);clearDraftSummary()", script)
        self.assertIn("if(state.reusedResult?.mode==='rerun'){showCreateStep(1,false);clearDraftSummary()", script)

    def test_canonical_routes_cover_all_spa_pages(self) -> None:
        routes = {route.path for route in app_module.app.routes}
        expected = {
            "/", "/create", "/flux-upscale", "/history", "/messages",
            "/messages/{conversation_id}", "/messages/{conversation_id}/info",
            "/messages/{conversation_id}/attachments/{attachment_id}", "/jobs/{prompt_id}",
            "/settings", "/settings/account", "/settings/templates", "/settings/help",
            "/settings/collections", "/settings/storage", "/settings/admin", "/login", "/register",
        }
        self.assertTrue(expected.issubset(routes), expected - routes)
        self.assertFalse(any(path == "/next" or path.startswith("/next/") for path in routes))

    def test_chat_layout_is_stable_in_the_canonical_bundle(self) -> None:
        canonical_html = (STATIC / "index.html").read_text(encoding="utf-8")
        shared_css = (STATIC / "style.css").read_text(encoding="utf-8")
        self.assertIn('id="view-messages"', canonical_html)
        self.assertIn(".chat-thread>header{grid-row:1}", shared_css)
        self.assertIn(".chat-message-list{grid-row:3}", shared_css)
        self.assertIn(".chat-thread>form{grid-row:4", shared_css)
        self.assertIn("#chat-thread-back{display:none}", shared_css)
        self.assertIn("#chat-share-dialog{width:min(760px", shared_css)

    def test_flux_upscale_shows_structured_workflow_stages(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        self.assertIn('id="flux-upscale-progress-stages"', html)
        self.assertIn("progress.stage_index", script)
        self.assertIn("progress.step_exact", script)
        self.assertIn("progress.tile_current", script)
        self.assertIn("task.label==='Flux 放大'", script)
        self.assertIn("'总进度'", script)
        self.assertIn("'当前块'", script)
        self.assertIn("'块内进度'", script)
        self.assertIn("stage.state||'pending'", script)
        self.assertIn(".flux-progress-stages", css)

    def test_chat_redesign_is_shared_and_keeps_dangerous_actions_out_of_header(self) -> None:
        canonical_html = (STATIC / "index.html").read_text(encoding="utf-8")
        script = (STATIC / "chat.js").read_text(encoding="utf-8")
        css = (STATIC / "chat-redesign.css").read_text(encoding="utf-8")
        self.assertIn('/static/chat-redesign.css?v=36', canonical_html)
        self.assertIn('id="chat-peer-open"', canonical_html)
        self.assertIn('id="chat-media-input"', canonical_html)
        self.assertNotIn('id="chat-friend-remove"', canonical_html)
        self.assertIn('/static/chat.js?v=28', canonical_html)
        self.assertIn('/static/app.js?v=60', canonical_html)
        self.assertIn('id="detail-share"', canonical_html)
        self.assertIn("async function shareJob(groupId,title='')", script)
        self.assertIn("filter(item=>item.friend&&!item.read_only)", script)
        self.assertIn("attachment:{kind:'task',group_id:groupId}", script)
        self.assertIn("route,shareJob", script)
        app_script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("ChatUI.shareJob(job.id,jobTitle(job))", app_script)

        self.assertIn("`已分享给 ${recipient.username}`", app_script)
        self.assertIn("share.hidden=true;share.onclick=null", app_script)
        self.assertIn("#chat-recipient-dialog", css)
        self.assertIn("grid-template-columns:repeat(4,minmax(0,1fr));gap:5px", css)
        self.assertNotIn(".detail-actions #detail-rerun{grid-column:1/-1}", css)
        self.assertIn("modelLabels:{}", script)
        self.assertIn("function registerWorkflows", script)
        self.assertIn("chat.modelLabels[option.value]=option.label", script)
        self.assertIn("while(spec.lora_aliases?.[current]&&!seen.has(current))", app_script)
        self.assertIn("name:resolveLoraName(item.name)", app_script)
        self.assertIn("keys=['h3_model','mode'", script)
        self.assertIn('id="chat-add-friend-mobile"', canonical_html)
        self.assertIn("q('#chat-add-friend-mobile').onclick=addFriendDialog", script)
        self.assertIn("/attachments/${button.dataset.chatOpen}", script)
        self.assertIn("/messages/${chat.current.id}/info", script)
        self.assertIn("selected.length>9", script)
        self.assertIn("chat-detail-gallery", script)
        self.assertIn(".chat-shell.conversation-open .chat-sidebar{display:none}", css)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", css)
        self.assertIn("chat-share-dialog-v3", script)
        self.assertIn("chat-attachment-detail-v3", script)
        self.assertIn("new IntersectionObserver", script)
        self.assertIn("function visibleMessageAnchor(root=q('#chat-message-list'))", script)
        self.assertIn("function restoreMessageAnchor(anchor)", script)
        self.assertIn("function stabilizeMessageAnchor(anchor", script)
        self.assertIn("function rememberAttachmentReturnAnchor()", script)
        self.assertIn("const insertionAnchor=mode==='older'||mode==='newer'?visibleMessageAnchor(list):null", script)
        self.assertIn("q('#view-messages')?.hidden", script)
        self.assertIn("messageResizeObserver", script)
        self.assertIn("first_unread_message_id", script)
        self.assertNotIn("heightBasedOpenConversation", script)
        self.assertIn("@media(min-width:760px){.chat-message{content-visibility:visible", css)
        self.assertIn(".chat-message{content-visibility:visible;contain:layout paint style;contain-intrinsic-size:none}", css)
        self.assertIn("body.chat-workspace-active.chat-keyboard-open{top:var(--chat-viewport-top,0);height:var(--chat-viewport-height,100dvh)}", css)
        self.assertNotIn("body.chat-workspace-active.chat-keyboard-open{top:0;", css)
        self.assertIn("body.chat-workspace-active.chat-keyboard-open .chat-shell{position:static;inset:auto;width:100%;height:100%;max-height:100%}", css)
        self.assertIn("body.chat-workspace-active .chat-thread>form>.chat-compose-row{display:grid;min-width:0", css)
        self.assertNotIn("仅发送首张图", script)
        self.assertIn("body.chat-workspace-active", css)
        self.assertIn("#chat-share-history{display:none!important}", css)
        self.assertIn("body.chat-workspace-active .mobile-header,body.chat-workspace-active .chat-page-heading,body.chat-workspace-active #server-banner{display:none!important}", css)
        self.assertIn("height:var(--chat-viewport-height,100svh)", css)
        self.assertIn("body.chat-workspace-active #view-messages:not([hidden]){display:grid;height:100%;grid-template-rows:minmax(0,1fr)}", css)
        self.assertIn("chat-conversation-active .assistive-nav:not(.keyboard-hidden)", css)
        self.assertIn("maxViewportHeight-height>120", script)
        self.assertIn("chat-keyboard-open", script)
        self.assertIn("image/gif", script)
        self.assertIn("已选择 1 个 GIF", script)
        self.assertNotIn("?download=true\" download>下载", script)

        media_branch = script.split("if(media){", 1)[1].split("const label=", 1)[0]
        self.assertIn("chat-inline-media-carousel", media_branch)
        self.assertIn("file.media_type==='image/gif'?raw", media_branch)
        self.assertNotIn("attachment.title", media_branch)
        self.assertNotIn("attachment.prompt", media_branch)
        self.assertIn(".chat-inline-media.single", css)
        self.assertIn(".chat-inline-media.carousel:hover .chat-inline-carousel-controls", css)
        self.assertIn(".chat-inline-carousel-controls>button{display:none}", css)
        self.assertIn(".chat-attachment.chat-inline-media>.chat-inline-media-carousel{display:flex", css)
        self.assertIn(".chat-attachment.chat-inline-media .chat-inline-media-slide{min-height:0;border:0;padding:0}", css)
        self.assertIn(".chat-attachment.chat-inline-media .chat-inline-media-slide>img", css)
        self.assertIn("object-fit:contain!important", css)
        self.assertIn("max-width:100%!important;max-height:100%!important", css)
        self.assertIn("aspect-ratio:var(--chat-media-aspect,4/5)", css)
        self.assertIn(".chat-inline-media.carousel .chat-inline-media-carousel{position:relative;max-height:none", css)
        self.assertIn(".chat-inline-media.carousel .chat-inline-media-slide>img{position:absolute;inset:0", css)
        self.assertIn(".chat-inline-media,.chat-inline-media.single{width:min(72vw,310px)}", css)
        self.assertIn(".chat-media-gallery{width:min(82vw,320px)", css)
        self.assertIn(".chat-message>div.chat-media-only{border:0!important", css)
        self.assertNotIn("carousel.style.height=", script)
        self.assertIn("--chat-media-aspect", script)
        self.assertIn(".chat-media-gallery .chat-detail-file>img", css)
        self.assertIn("height:auto;max-height:none;aspect-ratio:auto", css)
        self.assertNotIn(".chat-detail-file a[download]{display:none!important}", css)
        self.assertIn('data-detail-download=', script)
        self.assertIn("window.saveCanvasMedia(download.dataset.detailDownload", script)

    def test_account_profile_and_chat_identity_ui_are_wired(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        app_script = (STATIC / "app.js").read_text(encoding="utf-8")
        chat_script = (STATIC / "chat.js").read_text(encoding="utf-8")
        chat_css = (STATIC / "chat-redesign.css").read_text(encoding="utf-8")
        self.assertIn('id="view-account"', html)
        self.assertIn('href="/settings/account"', html)
        self.assertIn('id="account-avatar-input"', html)
        self.assertIn('id="account-profile-form"', html)
        self.assertIn('id="account-password-form"', html)
        self.assertNotIn('id="account-profile-password"', html)
        self.assertIn('data-icon-before="log-out"', html)
        self.assertIn("if(path==='/settings/account')return'account'", app_script)
        self.assertIn("openCropEditor('avatar')", app_script)
        self.assertIn("function avatarCropAction", app_script)
        self.assertIn("function resizeAvatarSelection", app_script)
        self.assertIn("centeredRatioSelection(ratioValue('1:1',image),.82)", app_script)
        self.assertNotIn("current_password:$('#account-profile-password').value", app_script)
        self.assertIn("/api/account/profile", app_script)
        self.assertIn("/api/account/password", app_script)
        self.assertIn('id="chat-peer-avatar"', html)
        self.assertIn("function setUserAvatar", chat_script)
        self.assertIn("data.type==='profile_updated'", chat_script)
        self.assertIn("message?.sender", chat_script)
        self.assertIn("class=\"chat-message-avatar chat-avatar\"", chat_script)
        self.assertIn(".chat-message.theirs.group-end>.chat-message-avatar", chat_css)

    def test_chat_uses_user_scoped_persistent_cache_and_network_revalidation(self) -> None:
        script = (STATIC / "chat.js").read_text(encoding="utf-8")
        app_script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("CHAT_CACHE_NAME='comfy-canvas-chat-v1'", script)
        self.assertIn("CHAT_CACHE_MESSAGE_LIMIT=200", script)
        self.assertIn("CHAT_CACHE_CONVERSATION_LIMIT=40", script)
        self.assertIn("indexedDB.open(CHAT_CACHE_NAME,CHAT_CACHE_VERSION)", script)
        self.assertIn("keyPath:['userId','conversationId','messageId']", script)
        self.assertIn("async function hydrateCachedOverview", script)
        self.assertIn("async function readCachedConversation", script)
        self.assertIn("scheduleConversationCache()", script)
        self.assertIn("await chat.cacheReady", script)
        self.assertIn("ensureOverview(true).catch", script)
        self.assertIn("clearLocalData:clearLocalChatData", script)
        self.assertIn("await ChatUI.clearLocalData?.()", app_script)
        self.assertNotIn("mediaFiles.forEach(file=>messages.put", script)

    def test_shared_tasks_continue_in_native_history_tools(self) -> None:
        chat_script = (STATIC / "chat.js").read_text(encoding="utf-8")
        self.assertIn("ensureSharedImported", chat_script)
        self.assertIn("chat_action:action", chat_script)
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("const chatImportedRenderDetail=renderDetail", script)
        self.assertIn("openDetailLightbox(job,index)", script)
        self.assertNotIn("openPromptAssistant(", script)

        self.assertIn("function sharedAttachmentRoute", script)
        self.assertIn("function loadSharedAttachmentDetail", script)
        self.assertIn("const sharedNativeRenderDetail=renderDetail", script)
        self.assertIn("if(current?.shared_context)return photoDetailMediaItems(current).length?[current]:[]", script)
        self.assertIn("shared-native-detail", script)
        self.assertIn("const sharedComparisonCardBase=comparisonCard", script)
        self.assertIn("function sharedSourceCompareToggle", script)
        self.assertIn("subject=imageMediaSemantics(job).compareSubject", script)
        self.assertNotIn("function sharedHistoryActions", script)
        self.assertIn("back.href=`/messages/${context.conversationId}`", script)
        self.assertIn("back.textContent='返回聊天'", script)
        self.assertIn('data-icon-before="arrow-left"', (STATIC / "index.html").read_text(encoding="utf-8"))

    def test_media_saves_do_not_replace_the_installed_app_page(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        guard = (STATIC / "mobile-media-guard.js").read_text(encoding="utf-8")
        self.assertIn("async function saveCanvasMedia", script)
        self.assertIn("URL.createObjectURL(blob)", script)
        self.assertIn("link.target='_blank'", script)
        self.assertIn("window.saveCanvasMedia=saveCanvasMedia", script)
        self.assertIn("photoDetailActionButton('保存/下载','download'", script)
        self.assertIn("typeof window.saveCanvasMedia!=='function'", guard)
        self.assertNotIn("if(download)link.download=''", guard)
        self.assertNotIn('/api/admin/ca-certificate', html)

    def test_image_roles_and_related_ui_vocabulary_are_canonical(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        css = (STATIC / "style.css").read_text(encoding="utf-8")

        self.assertIn("function imageMediaSemantics(job={})", script)
        self.assertIn("resultLabel:'原图'", script)
        self.assertIn("sourceLabel:'原图'", script)
        self.assertIn("resultLabel:'编辑结果'", script)
        self.assertIn("sourceLabel:'身份参考图'", script)
        self.assertIn("sourceLabel:'场景参考图'", script)
        self.assertIn("referenceLabel:'人物参考图'", script)
        self.assertIn("resultLabel:'生成结果'", script)
        self.assertIn("mode=modeField?.value||'edit'", script)
        self.assertIn("parameters:{reference_mode:mode}", script)
        self.assertIn("label:imageMediaSemantics(job).resultLabel", script)
        self.assertIn("label:semantics.resultLabel", script)
        self.assertIn("MEDIA_LABELS.detail", script)
        self.assertIn("MEDIA_LABELS.seedvr2", script)
        self.assertIn("MEDIA_LABELS.flux2", script)
        self.assertNotIn("Flux Add Detail", script)
        self.assertNotIn("'Stage 1'", script)
        self.assertIn("场景参考图", html)
        self.assertIn("人物参考图", html)

        self.assertNotIn("lora-guide-detail-filename", html)
        self.assertNotIn("lora-guide-detail-filename", script)
        self.assertNotIn("lora-guide-detail-filename", css)
        self.assertIn("search_text:`${name} ${display_name}", script)
        self.assertIn('id="lora-meta-path"', html)
        self.assertIn(":root{--text:var(--ink)}", css)

    def test_canonical_bundle_has_no_version_switch(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("data-version-switch", html)
        self.assertNotIn("返回经典版", html)
        self.assertNotIn("切换到 Next 版", html)
        self.assertNotIn("updateVersionSwitchLinks", script)

    def test_canonical_desktop_navigation_has_aligned_hover_feedback(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        icons = (STATIC / "icons.js").read_text(encoding="utf-8")
        self.assertIn("[data-icon]", icons)
        self.assertIn(".canvas-ui .primary-nav a:hover", css)
        self.assertIn("transform: translateX(3px)", css)
        self.assertIn(".canvas-ui .side-nav > .account-chip { margin: auto 8px 10px; }", css)

    def test_canonical_bundle_uses_one_local_lucide_icon_system(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        app_script = (STATIC / "app.js").read_text(encoding="utf-8")
        chat_script = (STATIC / "chat.js").read_text(encoding="utf-8")
        icons = (STATIC / "icons.js").read_text(encoding="utf-8")
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        self.assertIn("Lucide Static v1.33.0", icons)
        self.assertIn("window.CanvasIcons", icons)
        self.assertIn("new MutationObserver", icons)
        for name in ("house", "wand-sparkles", "maximize-2", "images", "message-circle", "settings"):
            self.assertIn(f'data-icon="{name}"', html)
            self.assertIn(f"'{name}':", icons)
        icon_references = set(
            re.findall(
                r"data-icon(?:-before|-after)?=[\"']([a-z0-9-]+)[\"']",
                html + app_script + chat_script,
            )
        )
        icon_references.update(
            ("star", "panel-right-open", "panel-right-close", "circle-check", "circle-x")
        )
        for name in icon_references:
            self.assertIn(f"'{name}':", icons)
        for fallback in (">⌂<", ">⇱<", ">▦<", ">◇<", ">⚙<", ">盾<"):
            self.assertNotIn(fallback, html)
        self.assertNotIn(".canvas-ui svg {", css)
        self.assertIn(".canvas-ui .ui-icon {", css)
        self.assertNotIn("<svg", html + app_script + chat_script)
        self.assertTrue((STATIC / "LUCIDE-LICENSE.txt").is_file())

    def test_mobile_icon_density_centering_and_disclosures_are_coherent(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        app_script = (STATIC / "app.js").read_text(encoding="utf-8")
        icons = (STATIC / "icons.js").read_text(encoding="utf-8")
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        chat_css = (STATIC / "chat-redesign.css").read_text(encoding="utf-8")

        self.assertIn(".canvas-ui button[data-icon]", css)
        self.assertIn("place-content: center", css)
        self.assertIn("#chat-media-pick{width:42px;height:42px", chat_css)
        self.assertIn('.chat-compose-actions button[type="submit"] [data-icon-slot="before"]', css)
        self.assertIn('.detail-actions [data-icon-slot="before"]', css)

        compact = app_script.split("function compactPostprocessPanel", 1)[1].split("upscalePanel=function", 1)[0]
        self.assertIn("summary.dataset.iconAfter='chevron-down'", compact)
        self.assertNotIn("badge", compact)
        self.assertIn(".postprocess-disclosure > summary[data-icon-after]", css)

        self.assertIn(".assistive-toggle > span::before", css)
        self.assertIn("#assistive-nav:not(.open) #assistive-message-badge", css)
        self.assertIn("#assistive-nav.open #assistive-toggle-badge", css)

        self.assertIn("'arrow-up-right':", icons)
        self.assertIn('data-icon="arrow-up-right"', app_script)
        self.assertNotIn("↗", app_script)
        self.assertIn('data-section="flux-upscale"><span data-icon="maximize-2"></span>放大</a>', html)
        self.assertIn('<small>放大</small>', html)
        self.assertIn("'flux-upscale':'放大'", app_script)

    def test_canonical_home_is_content_first_and_preserves_full_artwork(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="home-greeting" class="home-masthead home-greeting"', html)
        self.assertNotIn('<h1>创作空间</h1>', html)
        self.assertIn('id="home-greeting-messages" href="/messages" data-nav', html)
        self.assertIn("function homeGreetingProfile", script)
        self.assertIn("greeting:'早上好'", script)
        self.assertIn("greeting:'中午好'", script)
        self.assertIn("greeting:'下午好'", script)
        self.assertIn("greeting:'晚上好'", script)
        self.assertIn("greeting:'夜深了'", script)
        self.assertNotIn("greeting:'夜深了，'", script)
        self.assertIn("onUnreadChange:renderHomeGreeting", script)
        self.assertIn("homeGreetingUnread>99?'99+'", script)
        self.assertIn('.home-masthead[data-period="morning"]::before', css)
        self.assertIn(".home-greeting-status a", css)
        self.assertIn('id="home-gallery-prev"', html)
        self.assertIn('id="home-gallery-next"', html)
        self.assertLess(html.index('class="home-zone home-recent"'), html.index('class="home-secondary"'))
        self.assertIn("/api/gallery?limit=6", script)
        self.assertIn("job.items?.find(entry=>entry.final)", script)
        self.assertIn("image.naturalWidth/image.naturalHeight", script)
        self.assertIn("scrollHomeGallery(-1)", script)
        self.assertIn("scrollHomeGallery(1)", script)
        self.assertNotIn("copy.innerHTML=", script.split("function resultTile", 1)[1].split("async function loadActivity", 1)[0])
        self.assertIn("display: flex; grid-template-columns: none", css)
        self.assertIn("scroll-snap-type: x proximity", css)
        self.assertIn("object-fit: contain", css)
        self.assertIn("grid-template-columns: minmax(0,2fr) minmax(300px,1fr)", css)
        self.assertIn(".home-gallery-paging { display: none; }", css)
        self.assertIn("grid-template-columns: repeat(2,minmax(0,1fr))", css)
        self.assertIn("order=['krea-turbo','krea-identity-edit','minimax-h3','qwen2511-modular-flux2']", script)
        self.assertLess(script.index("order.forEach"), script.index("root.append(homeWorkflowCard('Flux 高清放大'"))
        workflow_card = script.split("function homeWorkflowCard", 1)[1].split("function renderWorkflowCards", 1)[0]
        self.assertNotIn("description", workflow_card)
        self.assertNotIn("workflow-card-kind", workflow_card)
        self.assertNotIn("<small>", workflow_card)
        self.assertIn("min-height: 80px", css)
        self.assertIn("min-height: 92px", css)

    def test_canonical_restores_classic_geometry_and_keeps_selected_enhancements(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        app_script = (STATIC / "app.js").read_text(encoding="utf-8")
        behavior = (STATIC / "interface.js").read_text(encoding="utf-8")
        icons = (STATIC / "icons.js").read_text(encoding="utf-8")
        self.assertEqual((STATIC / "style.css").read_bytes(), (STATIC / "style.css").read_bytes())
        self.assertNotIn('next-sidebar-toggle', html)
        self.assertNotIn('sidebar-expanded', behavior)
        self.assertIn('#create-step-1 .create-workbench', css)
        self.assertIn('grid-template-columns: minmax(340px,.72fr) minmax(0,1.28fr)', css)
        self.assertIn('#create-step-2 > .lora-section { position: static', css)
        self.assertIn('#flux-upscale-form .flux-workbench', css)
        self.assertIn('position: sticky; z-index: 12; bottom: 0', css)
        self.assertIn("const CREATE_GROUP_STATE_KEY='nextCreateGroupStateV1'", app_script)
        self.assertIn("new Set(['sampling','edit','advanced','edit_advanced'])", app_script)
        self.assertIn("rememberCreateGroup(spec.key,group.id,box.open)", app_script)
        self.assertIn("label.dataset.field=name", app_script)
        self.assertIn("function loraTableHeader()", app_script)
        self.assertIn("row.append(slot,nameLabel,weightLabel)", app_script)
        self.assertIn('class="create-meta-grid"', html)
        self.assertIn('class="draft-summary create-context-bar"', html)
        self.assertIn('class="flow-section-heading"', html)
        self.assertIn('grid-template-columns: 180px minmax(0,1fr)', css)
        self.assertIn('grid-template-columns: repeat(12,minmax(0,1fr))', css)
        self.assertIn('grid-template-columns: 42px minmax(0,1fr) minmax(76px,92px)', css)
        self.assertIn(':has(#upload-preview[hidden]) #image-field', css)
        self.assertIn('place-content: center', css)
        self.assertIn('--canvas-indigo: #8179e8', css)
        self.assertIn('--canvas-ice: #9cc8ff', css)
        self.assertIn('@media (max-width: 1099px)', css)
        self.assertIn('.assistive-nav', css)
        self.assertIn('.detail-lightbox', css)
        self.assertIn('class="viewer-toolbar-group viewer-zoom-tools"', html)
        self.assertIn('.detail-lightbox-inspector .enhanced-controls', css)
        self.assertIn('grid-template-columns: repeat(3,minmax(0,1fr))', css)
        self.assertIn("controls.append(field('图片类型',styleProfile),field('目标长边',resolution),field('LoRA 强度',strength),button)", app_script)
        self.assertIn("flux-upscale-strength", app_script)
        self.assertIn("'#detail-lightbox-next'", behavior)
        self.assertIn("const icons = Object.freeze", icons)
        self.assertIn('canvas-route-enter', behavior)
        self.assertIn('prefers-reduced-motion', css)

    def test_canonical_history_is_order_preserving_full_frame_masonry(self) -> None:
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        behavior = (STATIC / "interface.js").read_text(encoding="utf-8")
        app_script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("canvas-history-active", css)
        self.assertIn("canvas-history-masonry", css)
        self.assertIn("object-fit: contain", css)
        self.assertIn("@media (max-width: 599px)", css)
        self.assertIn("grid-template-columns: repeat(2,minmax(0,1fr))", css)
        self.assertIn("#view-history .filter-bar", css)
        self.assertIn("const lane = index % columns", behavior)
        self.assertIn("if (width < 600) return 1", behavior)
        self.assertIn("if (width < 920) return 2", behavior)
        self.assertIn("if (width < 1400) return 3", behavior)
        self.assertIn("if (width < 1800) return 4", behavior)
        self.assertNotIn("column-count", css)
        self.assertNotIn("Math.min(...heights)", behavior)
        history_parameters = app_script.split("function historyParameterSummary", 1)[1].split("function renderDetailParameters", 1)[0]
        self.assertIn("[...view.core,...view.sampling]", history_parameters)
        self.assertIn("view.loras.forEach", history_parameters)
        self.assertNotIn("view.seed", history_parameters)
        self.assertNotIn("view.actualSeeds", history_parameters)

    def test_canonical_history_overlay_adapts_copy_without_inner_scroll(self) -> None:
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        behavior = (STATIC / "interface.js").read_text(encoding="utf-8")
        self.assertIn("background: linear-gradient(180deg, rgba(5,8,18,.78), rgba(5,8,18,.96))", css)
        self.assertIn("font-size: .86rem", css)
        self.assertIn("font-size: .73rem", css)
        self.assertIn("white-space: pre-wrap", css)
        self.assertIn("overflow: hidden", css)
        self.assertIn("-webkit-line-clamp: var(--history-prompt-lines,4)", css)
        self.assertIn("history-compact", css)
        self.assertIn("history-minimal", css)
        self.assertIn("history-actions-only", css)
        self.assertIn("const fitHistoryCardOverlay", behavior)
        self.assertIn("availablePromptHeight", behavior)
        history_body = css.split(".canvas-history-masonry .history-body {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow: hidden", history_body)
        self.assertNotIn("overflow-y: auto", history_body)

    def test_canonical_mobile_parameter_mode_caps_long_prompts(self) -> None:
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        self.assertIn("canvas-history-mode-params", css)
        self.assertIn("-webkit-line-clamp: 5", css)
        self.assertIn("-webkit-line-clamp: 7", css)
        self.assertIn("@media (min-width: 420px) and (max-width: 599px)", css)

    def test_canonical_lora_guide_uses_compact_contained_rows(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        base_css = (STATIC / "style.css").read_text(encoding="utf-8")
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="lora-guide-detail"', html)
        self.assertIn('id="lora-guide-detail-back"', html)
        self.assertIn('data-lora-filter="favorite"', html)
        self.assertIn('id="lora-meta-delete"', html)
        self.assertIn('<option value="stale">文件已失效</option>', html)
        self.assertIn('aria-hidden="true"', html)
        self.assertIn("#lora-guide-dialog .lora-guide-list", css)
        self.assertIn("align-content: start", css)
        self.assertIn("grid-auto-rows: max-content", css)
        self.assertIn("grid-template-columns: minmax(0,1fr) auto", css)
        self.assertIn("width: calc(100vw - var(--lora-safe-inline) - var(--lora-safe-inline))", css)
        self.assertIn("overflow-x: hidden", css)
        self.assertIn("content-visibility: auto", css)
        self.assertIn("backdrop-filter: none", css)
        self.assertIn("document.createDocumentFragment()", script)
        self.assertIn("appendLoraGuideBatch", script)
        self.assertIn("state.loraGuideCatalog", script)
        self.assertIn("$('#lora-guide-list').onclick=", script)
        self.assertIn("input.dataset.loraWeight=item.name", script)
        self.assertIn("selected.weight.value=input.value", script)
        self.assertIn("remove.dataset.loraAction='remove'", script)
        self.assertIn("main.dataset.loraAction='toggle'", script)
        self.assertIn("main.setAttribute('aria-pressed',String(isSelected))", script)
        self.assertIn("favorite.dataset.icon='star'", script)
        self.assertIn("async function toggleLoraFavorite", script)
        self.assertIn("async function deleteStaleLoraMetadata", script)
        self.assertIn("state.loraFavorites=new Set(help.lora_favorites||[])", script)
        self.assertIn("card.classList.contains('selected')", script)
        self.assertIn("index>=6?' lora-overflow-slot':''", script)
        self.assertIn("row.hidden=index>=6&&!preset.name", script)
        self.assertIn("function syncLoraSlotVisibility()", script)
        self.assertIn("expanded=selected>6", script)
        self.assertIn("function loraSelectionCapacity()", script)
        self.assertIn("还可选择 ${capacity.remaining} 个 LoRA", script)
        self.assertIn("aria-live','polite", script)
        self.assertIn(".lora-guide-capacity", css)
        self.assertIn("#loras .lora-row[hidden]", css)
        self.assertNotIn("function signedNumberControl", script)
        self.assertNotIn("signed-number-sign", base_css)
        self.assertIn("weight.type='text';weight.inputMode='text'", script)
        self.assertIn("input.type='text';input.inputMode='text'", script)
        description_tag = html.split('id="lora-meta-description"', 1)[1].split(">", 1)[0]
        self.assertNotIn("maxlength", description_tag)
        self.assertIn("function openLoraGuideDetail", script)
        self.assertIn("function closeLoraGuideDetail", script)
        self.assertIn("function setLoraGuideBrowserInert", script)
        self.assertIn("kind==='detail'", script)
        self.assertIn("select.dataset.loraAction='quick-select'", script)
        self.assertIn("input.type='text';input.inputMode='text'", script)
        self.assertIn("kind==='quick-select'&&card.classList.contains('selected')", script)
        self.assertIn("grid-template-columns: minmax(0,1fr) auto auto", css)
        self.assertIn(".lora-guide-view-switch { grid-column: 1 / -1", css)
        self.assertIn(".lora-guide-toolbar { grid-template-columns: minmax(0,1fr) minmax(108px,.65fr)", css)
        self.assertIn(".lora-guide-toolbar > .segmented { grid-column: 1 / -1", css)
        self.assertIn("event.preventDefault();closeLoraGuideDetail()", script)
        self.assertIn("-webkit-line-clamp:3", base_css)
        self.assertIn("-webkit-line-clamp:2", base_css)
        self.assertIn(".lora-guide-panel.detail-open .lora-guide-detail", base_css)
        self.assertIn("transform:translateX(100%)", base_css)
        self.assertIn("@media(prefers-reduced-motion:reduce)", base_css)
        self.assertIn(".lora-guide-card-main { padding: 14px 15px; }", css)
        self.assertIn(".lora-guide-card-main:focus-visible { outline: none; }", css)
        self.assertIn("-webkit-tap-highlight-color: transparent", css)
        self.assertIn(".lora-guide-card:has(.lora-guide-card-main:focus-visible)", css)
        self.assertIn("grid-template-columns: repeat(3,minmax(0,1fr))", css)

    def test_history_recycle_bin_uses_aligned_icons_and_restore_only_cards(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        style = (STATIC / "style.css").read_text(encoding="utf-8")
        trash_index = html.index('id="history-recycle-open"')
        refresh_index = html.index('id="history-refresh"')
        self.assertLess(trash_index, refresh_index)
        self.assertIn('id="view-recycle-bin"', html)
        self.assertIn('href="/history/trash"', html)
        self.assertNotIn('id="recycle-refresh"', html)
        self.assertNotIn("$('#recycle-refresh')", script)
        self.assertIn("width:40px;height:40px", style)
        self.assertIn("function recycleCountdown", script)
        self.assertIn("setInterval(updateRecycleCountdowns,1000)", script)
        self.assertIn("/api/recycle-bin/${encodeURIComponent(entry.id)}/restore", script)
        recycle_view = html.split('id="view-recycle-bin"', 1)[1].split("</section>", 1)[0]
        recycle_card = script.split("function recycleCard", 1)[1].split("function renderRecycleBin", 1)[0]
        self.assertNotIn("清空回收站", recycle_view + recycle_card)
        self.assertNotIn("永久删除", recycle_view + recycle_card)
        self.assertNotIn("method:'DELETE'", recycle_card)

    def test_history_delete_preserves_loaded_view_and_scroll_position(self) -> None:
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        move = script.split("async function moveJobToRecycle", 1)[1].split("async function moveImageToRecycle", 1)[0]
        self.assertIn("state.historyOffset=Math.max(0,state.historyOffset-1)", move)
        self.assertIn("await historyFrame();restorePagePosition(path,left,top)", move)
        self.assertNotIn("resetHistory()", move)

    def test_data_dependent_dialogs_open_before_waiting_for_network(self) -> None:
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        account = script.split("async function openAccount()", 1)[1].split("async function logout", 1)[0]
        self.assertLess(account.index("dialog.showModal()"), account.index("await api('/api/account/storage')"))
        self.assertNotIn("async function openPromptAssistant", script)
        crop = script.split("async function openCropEditor", 1)[1].split("function cropPointer", 1)[0]
        self.assertLess(crop.index("dialog.showModal()"), crop.index("await loadEditorImage(src)"))

    def test_canonical_mobile_assistive_menu_uses_edge_tab_and_responsive_drag(self) -> None:
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("const ASSISTIVE_SIZE=48", script)
        self.assertIn("Math.hypot(dx,dy)<4", script)
        self.assertIn("const animation=root.animate", script)
        self.assertIn("fill:'none'", script)
        self.assertIn("stopSnapAtCurrent", script)
        self.assertIn("targetTop=Math.max", script)
        self.assertNotIn("transform:`translateX(${deltaX}px)`", script)
        self.assertIn("cubic-bezier(.2,.8,.2,1)", script)
        self.assertIn(".assistive-nav.snapping { transition: none; }", css)
        self.assertIn(".assistive-nav.side-right:not(.dragging) .assistive-toggle", css)
        self.assertIn(".assistive-nav.side-left:not(.dragging) .assistive-toggle", css)
        self.assertIn(".assistive-toggle > span .ui-icon", css)
        self.assertIn(".assistive-nav.open .assistive-toggle > span", css)
        self.assertIn("dataset.icon='menu'", script)
        self.assertIn("dataset.icon=assistiveOpen?'x':'menu'", script)
        self.assertIn("backdrop-filter: none", css)

    def test_shared_source_scroll_and_assistive_snap_use_stable_mobile_geometry(self) -> None:
        shared_css = (STATIC / "chat-redesign.css").read_text(encoding="utf-8")
        self.assertIn(".shared-native-detail .source-panel .source-image{contain:layout paint;touch-action:pan-y", shared_css)
        self.assertIn("transform:none!important;transition:none!important", shared_css)
        self.assertIn(".shared-native-detail .source-panel .source-image{display:grid;width:100%;height:auto", shared_css)
        self.assertIn("height:auto;max-height:none;object-fit:contain", shared_css)
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("function stabilizeSharedSourceTouch(link)", script)
        self.assertIn("suppressUntil=performance.now()+500", script)
        active = script.split("initAssistiveNavigation=function(){", 1)[1].split("let actionStatusTimer", 1)[0]
        self.assertIn("root.classList.add('snapping');applyAssistivePosition(position);const target=root.getBoundingClientRect()", active)
        self.assertIn("{transform:'none'}", active)
        self.assertIn("fill:'none'", active)
        self.assertNotIn("left:`${targetLeft}px`,top:`${targetTop}px`", active)

    def test_canonical_history_has_mobile_modes_and_direct_image_navigation(self) -> None:
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        behavior = (STATIC / "interface.js").read_text(encoding="utf-8")
        self.assertIn("nextHistoryMobileMode", behavior)
        self.assertIn("['clean', '纯净版']", behavior)
        self.assertIn("['params', '带参数']", behavior)
        self.assertNotIn("menu.className = 'history-card-menu'", behavior)
        self.assertIn("canvas-history-mode-clean", css)
        self.assertIn("canvas-history-mode-params", css)
        self.assertIn(".history-card.canvas-overlay-open .history-params { display: grid; }", css)
        self.assertIn("fitHistoryCardOverlay(card)", behavior)
        self.assertIn("position: relative", css)
        self.assertIn("const cleanMobileTap = (historyTouchLayoutEnabled() ? historyMobileColumns === 1 : window.innerWidth < 600)", behavior)
        self.assertIn("const historyPreviewTap = historyCard && link.matches('a.history-media')", behavior)
        self.assertIn("if (historyPreviewTap) return", behavior)
        self.assertIn("if (cleanMobileTap && openHistoryCard !== card)", behavior)
        self.assertIn("media.setAttribute('aria-expanded', 'true')", behavior)
        self.assertIn("if (media) closeHistoryOverlay()", behavior)
        self.assertNotIn("menu.textContent = '•••'", behavior)

    def test_canonical_touch_history_pinches_between_clean_masonry_densities(self) -> None:
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        behavior = (STATIC / "interface.js").read_text(encoding="utf-8")
        self.assertIn("nextHistoryMobileColumnsV1", behavior)
        self.assertIn("(hover: none) and (pointer: coarse)", behavior)
        self.assertIn("if (historyTouchLayoutEnabled()) return historyMobileColumns", behavior)
        self.assertIn("columns === 1 ? 12 : columns === 2 ? 8 : 6", behavior)
        self.assertIn("calculateHistoryPreviewLayout", behavior)
        self.assertIn("snapshot.className = 'history-pinch-snapshot'", behavior)
        self.assertIn("const historyExitDistance = () => clamp(window.innerWidth * .0615, 20, 28)", behavior)
        self.assertIn("const historyLayerDistance = () => clamp(window.innerWidth * .215, 76, 96)", behavior)
        self.assertIn("const historyGestureState = (pinch, intent)", behavior)
        self.assertIn("Math.max(0, travel - exitDistance) / layerDistance", behavior)
        self.assertIn("if (availableDistance < 52) layerDistance = Infinity", behavior)
        self.assertIn("Math.max(52, Math.min(baseBoundary, availableDistance * .9))", behavior)
        self.assertIn("const imageFadeEnd = exitDistance + visualLayerDistance * .62", behavior)
        self.assertIn("Math.pow(clamp(travel / imageFadeEnd, 0, 1), 2)", behavior)
        self.assertIn("const guideFadeStart = exitDistance * .2", behavior)
        self.assertIn("const guideFadeEnd = exitDistance + visualLayerDistance * .3", behavior)
        self.assertNotIn("historyDensityRatio", behavior)
        self.assertNotIn("magneticHistoryColumns", behavior)
        self.assertIn("const displayColumns = rawColumns", behavior)
        self.assertIn("Math.round(displayColumns)", behavior)
        self.assertNotIn("historyZoomInTrigger", behavior)
        self.assertNotIn("historyStageTrigger", behavior)
        self.assertNotIn("previewStages", behavior)
        self.assertNotIn("projectedHistoryRatio", behavior)
        self.assertNotIn("sweptHistoryPinchRect", behavior)
        self.assertIn("rectIntersectsPinchBuffer(transformed)", behavior)
        self.assertIn("ensureHistorySnapshotCoverage", behavior)
        self.assertIn("image.loading = 'eager'", behavior)
        self.assertIn("image.decode?.().catch(() => {})", behavior)
        self.assertNotIn("const ratioSamples =", behavior)
        self.assertNotIn("anchor.className = 'history-pinch-anchor'", behavior)
        self.assertIn("const layouts = [null], sourceRects = cachedHistorySourceRects", behavior)
        self.assertNotIn("calculateHistoryPreviewLayout(cards, 1, galleryRect),", behavior)
        self.assertIn("snapshot.hidden = false", behavior)
        self.assertIn("transformOrigin = `${pinch.startMidpoint.x}px ${pinch.startMidpoint.y}px`", behavior)
        self.assertIn("scale(${liveScale})", behavior)
        self.assertNotIn("targetSnapshot", behavior)
        self.assertNotIn("snapshotTransforms", behavior)
        self.assertNotIn("snapshotPlacements", behavior)
        self.assertIn("historyGallery.style.setProperty('--history-pinch-crossfade', '1')", behavior)
        self.assertIn("const { rawColumns, imageFadeProgress } = gestureState", behavior)
        self.assertIn("entry.tile.style.opacity = String(1 - imageFadeProgress)", behavior)
        self.assertIn("syncHistoryColumnGuides(pinch, rawColumns, gestureState.guideProgress, gestureState.direction)", behavior)
        self.assertIn("syncHistoryDensitySlider(pinch, displayColumns, null, gestureState)", behavior)
        self.assertIn("--history-exit-progress", behavior)
        self.assertIn("is-preparing", behavior)
        self.assertIn("history-pinch-density-boundary", behavior)
        self.assertIn("pinch.slider.classList.toggle('is-changing'", behavior)
        self.assertIn("if (!pinch.moved)", behavior)
        self.assertIn("renderHistoryPinch(pinch)", behavior)
        self.assertIn("cachedHistorySourceRects", behavior)
        self.assertIn("card.style.getPropertyValue('--history-card-width')", behavior)
        self.assertIn("if (imageFadeProgress < .98) ensureHistorySnapshotCoverage", behavior)
        self.assertIn("event.preventDefault();\n      if (!historyPinch) createHistoryPinchLayer(points, 'touch')", behavior)
        self.assertIn("const canCommit = target !== pinch.startColumns", behavior)
        self.assertIn("commit ? pinch.visual.selectedColumns : pinch.startColumns", behavior)
        self.assertIn("canCommit ? 420 : 320", behavior)
        self.assertIn("const backgroundProgress = smootherstep(clamp((progress - .06) / .94", behavior)
        self.assertIn("pinch.snapshot.style.opacity = String(1 - backgroundProgress)", behavior)
        self.assertIn("historyGallery.style.setProperty('--history-pinch-crossfade', String(1 - backgroundProgress))", behavior)
        self.assertIn("const guideHandoffOpacity = set === targetGuide ? .3 : 0", behavior)
        self.assertIn("const outlineCandidates = pinch.cards.map", behavior)
        self.assertIn("visibleRect.y + visibleRect.height <= 0 || visibleRect.y >= window.innerHeight", behavior)
        self.assertIn("const outlinePropagation = reduceMotion || outlineCandidates.length < 2 ? 0 : 180", behavior)
        self.assertIn("const outlineRise = reduceMotion ? 0 : 90", behavior)
        self.assertIn("const outlineHold = reduceMotion ? 0 : 100", behavior)
        self.assertIn("const outlineFade = reduceMotion ? 120 : 400", behavior)
        self.assertIn("const outlineMovesOutward = target > pinch.startColumns", behavior)
        self.assertIn("outlinePropagation * waveProgress", behavior)
        self.assertIn("visibleRect.width / 2 - visual.midpoint.x", behavior)
        self.assertIn("--history-pinch-outline-local-opacity", behavior)
        self.assertIn("--history-pinch-outline-brightness", behavior)
        self.assertIn("--history-pinch-outline-opacity", behavior)
        self.assertIn("1 - easeInOutCubic(fadeProgress)", behavior)
        self.assertNotIn("const focalProgress", behavior)
        self.assertNotIn("mix(anchorRect.left", behavior)
        self.assertNotIn("mix(anchorRect.top", behavior)
        self.assertNotIn("mix(anchorRect.width", behavior)
        self.assertNotIn("mix(anchorRect.height", behavior)
        self.assertIn("scale(${mix(fromTransform.scale, 1, eased)})", behavior)
        self.assertNotIn("activeColumns", behavior.split("const historyPinchIntent", 1)[1].split("const supportsNativeTouch", 1)[0])
        self.assertNotIn("blur(16px)", behavior)
        self.assertNotIn("densityRect", behavior)
        self.assertIn("historySuppressClickUntil = performance.now() + 450", behavior)
        self.assertIn("applyHistoryColumns(target, { immediate: true })", behavior)
        self.assertIn("restoreHistoryAnchor(anchor)", behavior)
        self.assertIn("nextMode === 'params' && historyTouchLayoutEnabled() && historyMobileColumns > 1", behavior)
        self.assertIn("historyMobileMode = 'clean'", behavior)
        self.assertIn("touch-action: pan-y", css)
        self.assertIn("canvas-history-density-expanded", css)
        self.assertIn(".canvas-history-masonry .history-body", css)
        self.assertIn("display: none !important", css)
        self.assertIn(".history-media > span { display: none !important; }", css)
        expanded_css = css.split("#view-history.canvas-history-density-expanded", 1)[1].split("}", 2)[0]
        self.assertNotIn("video-play-mark", expanded_css)
        self.assertIn("history-pinch-layer", css)
        self.assertIn("history-pinch-snapshot", css)
        self.assertNotIn("history-pinch-anchor", css)
        self.assertIn("history-pinch-tile", css)
        self.assertIn("history-pinch-backdrop", css)
        self.assertIn("history-pinch-density-slider", css)
        self.assertIn("history-pinch-density-thumb", css)
        self.assertIn("history-pinch-density-boundary", css)
        self.assertIn("history-card.history-pinch-confirmed::after", css)
        self.assertIn("var(--history-pinch-outline-opacity,var(--history-pinch-outline-local-opacity,0))", css)
        self.assertNotIn("history-pinch-guide-set.is-residual", css)
        self.assertIn("history-pinch-density-slider.is-preparing", css)
        self.assertIn("history-pinch-guides", css)
        self.assertIn("history-pinch-guide-set", css)
        self.assertIn(".history-pinch-guide-set > i::before", css)
        self.assertIn(".history-pinch-guide-set > i::after", css)
        self.assertIn("top: 7%", css)
        self.assertIn("bottom: 7%", css)
        self.assertIn("safe-area-inset-top", css)
        self.assertNotIn("history-pinch-focus-only", css)
        self.assertIn("createHistoryDensitySlider", behavior)
        self.assertIn("syncHistoryDensitySlider", behavior)
        self.assertIn("createHistoryColumnGuides", behavior)
        self.assertIn("syncHistoryColumnGuides", behavior)
        self.assertIn("grid-template-columns: repeat(var(--history-guide-columns)", css)
        self.assertIn(".history-pinch-snapshot", css)
        self.assertIn("will-change: transform,opacity", css)
        self.assertIn("--history-pinch-crossfade", css)
        self.assertIn("historyGallery.addEventListener('touchstart'", behavior)
        self.assertIn("'gesturestart', 'gesturechange', 'gestureend'", behavior)
        self.assertNotIn("history-density-notice", css)

    def test_canonical_history_auto_loads_near_the_end_on_desktop_and_mobile(self) -> None:
        behavior = (STATIC / "interface.js").read_text(encoding="utf-8")
        self.assertIn("new IntersectionObserver", behavior)
        self.assertIn("rootMargin: '500px 0px'", behavior)
        self.assertIn("Promise.resolve(loadHistory())", behavior)
        observer_block = behavior.split("if (historyLoadMore && 'IntersectionObserver' in window)", 1)[1].split("if (historyView)", 1)[0]
        self.assertNotIn("min-width: 920px", observer_block)

    def test_canonical_history_limits_scroll_rendering_cost(self) -> None:
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        behavior = (STATIC / "interface.js").read_text(encoding="utf-8")
        app_script = (STATIC / "app.js").read_text(encoding="utf-8")
        history_card_css = css.split(".canvas-history-masonry > .history-card {", 1)[1].split("}", 1)[0]
        self.assertIn("content-visibility: auto", history_card_css)
        self.assertIn("contain-intrinsic-size: auto 320px", history_card_css)
        self.assertNotIn("will-change", history_card_css)
        self.assertIn("historyLayoutKey", behavior)
        self.assertIn("card.dataset.historyHeight", behavior)
        self.assertIn("card.style.left = `${left}px`", behavior)
        self.assertIn("card.style.top = `${top}px`", behavior)
        self.assertNotIn("card.style.transform = `translate(", behavior)
        self.assertIn("historyGallery.addEventListener('pointerover'", behavior)
        self.assertIn("history-scroll-active", behavior)
        self.assertIn("history-fit-ready", behavior)
        self.assertIn("history-nearby", css)
        self.assertIn("rootMargin: '1600px 0px'", behavior)
        self.assertIn("image.decode?.()", behavior)
        self.assertIn("maxSize<=512", app_script)
        history_card = app_script.split("function historyCard", 1)[1].split("function actionButton", 1)[0]
        self.assertIn(",512);image.fetchPriority='low'", history_card)

    def test_canonical_history_restores_the_opened_card_after_detail(self) -> None:
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("nextHistoryReturnV1", script)
        self.assertIn("history.scrollRestoration='manual'", script)
        self.assertIn("function captureHistoryReturn", script)
        self.assertIn("function restoreHistoryView", script)
        self.assertIn("HISTORY_RESTORE_PAGE_LIMIT=50", script)
        self.assertIn("await historyImageReady(card.querySelector('.history-media img'))", script)
        self.assertIn("stableFrames>=3", script)
        self.assertIn("Math.abs(error)<.5", script)
        self.assertIn("card.getBoundingClientRect().top", script)
        self.assertIn("returningToHistory", script)
        self.assertIn("if(key!=='detail'&&!photoDetailUi.shell.hidden)photoDetailDeactivate()", script)
        self.assertIn("await restoreHistoryView()", script)
        self.assertNotIn("new MutationObserver(()=>{if($('#view-detail').hidden", script)

    def test_canonical_history_cover_advances_after_the_first_item_is_deleted(self) -> None:
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("function historyCoverItem", script)
        self.assertIn("find(item=>item?.final)", script)
        self.assertIn("media.dataset.coverItemId=String(item.id)", script)
        self.assertIn("image.dataset.itemId=String(item.id)", script)
        self.assertIn("function historyRefreshCard", script)
        self.assertIn("if(refreshedJob)historyRefreshCard(refreshedJob)", script)

    def test_canonical_mobile_parameter_cards_keep_real_content_height(self) -> None:
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        behavior = (STATIC / "interface.js").read_text(encoding="utf-8")
        self.assertIn("#view-history.canvas-history-mode-params .canvas-history-masonry > .history-card", css)
        self.assertIn("content-visibility: visible", css)
        self.assertIn("contain-intrinsic-size: none", css)
        self.assertIn("historyMobileMode === 'params') card.classList.add('history-fit-ready'", behavior)

    def test_history_detail_mobile_image_actions_use_one_non_overlapping_tray(self) -> None:
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        self.assertIn("tray.className='mobile-media-actions'", script)
        self.assertIn("tray.append(...mediaActions)", script)
        self.assertIn("grid-auto-flow: column", css)
        self.assertIn("grid-auto-columns: minmax(0,1fr)", css)
        self.assertIn(".mobile-controls-card.compare > .mobile-media-actions { bottom: 46px; }", css)
        self.assertIn('.mobile-media-actions [data-icon-slot="before"] { display: none; }', css)

    def test_canonical_secondary_text_tokens_meet_normal_text_contrast(self) -> None:
        css = (STATIC / "interface.css").read_text(encoding="utf-8")

        def token(name: str) -> str:
            match = re.search(rf"--{re.escape(name)}:\s*#([0-9a-fA-F]{{6}})", css)
            self.assertIsNotNone(match, name)
            return match.group(1)

        def luminance(value: str) -> float:
            channels = [int(value[index:index + 2], 16) / 255 for index in (0, 2, 4)]
            converted = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
            return 0.2126 * converted[0] + 0.7152 * converted[1] + 0.0722 * converted[2]

        panel = luminance(token("panel"))
        for name in ("ink", "muted", "soft"):
            foreground = luminance(token(name))
            ratio = (max(panel, foreground) + 0.05) / (min(panel, foreground) + 0.05)
            self.assertGreaterEqual(ratio, 4.5, f"{name}: {ratio:.2f}")

    def test_canonical_buttons_use_low_saturation_semantic_states(self) -> None:
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        general_button = css.split(".canvas-ui button,", 1)[1].split("}", 1)[0]
        self.assertIn("background: rgba(36, 45, 70, .88)", general_button)
        self.assertNotIn("#b7d8ff", general_button)
        self.assertIn(".canvas-ui .danger", css)
        self.assertIn("rgba(94,42,54,.34)", css)

    def test_seed_modes_and_compact_lora_selector_are_exposed(self) -> None:
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        css = (STATIC / "interface.css").read_text(encoding="utf-8")
        self.assertIn("new Option('Seed 不变','fixed'", script)
        self.assertIn("new Option('Seed 递增','increment'", script)
        self.assertIn("new Option('Seed 递减','decrement'", script)
        self.assertIn("data-lora-quick-weight", script)
        self.assertIn('data-lora-view="compact"', html)
        self.assertIn('id="lora-guide-category"', html)
        self.assertIn(".lora-guide-list.compact", css)

    def test_lora_category_admin_controls_are_present(self) -> None:
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="lora-category-new-form"', html)
        self.assertIn('id="lora-meta-category"', html)
        self.assertIn("async function deleteLoraCategory", script)
        self.assertIn("uncategorized", script)

    def test_private_media_cache_policy_is_bounded_and_cookie_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = STATIC / "icons" / "fraunces-app-v1-192.png"
            with patch.object(app_module, "THUMBNAIL_DIR", Path(temporary)):
                preview = asyncio.run(app_module.private_image_response(source, 512))
                original = asyncio.run(app_module.private_image_response(source))
        self.assertEqual(
            preview.headers["cache-control"],
            "private, max-age=604800, stale-while-revalidate=86400",
        )
        self.assertEqual(preview.headers["vary"], "Cookie")
        self.assertEqual(original.headers["cache-control"], "private, no-cache, max-age=0, must-revalidate")
        self.assertEqual(original.headers["vary"], "Cookie")

    def test_static_cache_policy_uses_content_hashes_instead_of_query_versions(self) -> None:
        async def cache_control(path: str, query: bytes = b"") -> str:
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "method": "GET",
                "scheme": "https", "path": path, "raw_path": path.encode(),
                "query_string": query, "headers": [], "client": ("127.0.0.1", 12345),
                "server": ("testserver", 443),
            }

            async def response(_request):
                return Response()

            result = await app_module.authentication_middleware(Request(scope), response)
            return result.headers["cache-control"]

        immutable = "public, max-age=31536000, immutable"
        self.assertEqual(
            asyncio.run(cache_control("/static/app.js", b"v=55")),
            "no-cache, max-age=0, must-revalidate",
        )
        self.assertEqual(asyncio.run(cache_control("/static/assets/app-AbCdEf1234.js")), immutable)
        self.assertEqual(asyncio.run(cache_control("/static/icons/fraunces-app-v1-192.png")), immutable)
        self.assertEqual(
            asyncio.run(cache_control("/static/unversioned.js")),
            "no-cache, max-age=0, must-revalidate",
        )
        self.assertEqual(asyncio.run(cache_control("/login")), "no-store, max-age=0")
        pages = (RUNTIME_STATIC / "index.html").read_text(encoding="utf-8") + (RUNTIME_STATIC / "auth.html").read_text(encoding="utf-8")
        self.assertNotIn("?v=", pages)
        for reference in re.findall(r'(?:src|href)="(/static/[^"#]+)', pages):
            path = reference
            if Path(path).suffix.lower() not in {".css", ".js"}:
                continue
            with self.subTest(reference=reference):
                self.assertTrue(app_module.HASHED_STATIC_FILE_RE.search(Path(path).name))


class CanonicalRedirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_bookmark_redirects_to_the_canonical_path(self) -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "GET",
            "scheme": "https",
            "path": "/next/history",
            "raw_path": b"/next/history",
            "query_string": b"workflow=minimax-h3&sort=oldest",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        }

        async def unexpected_call_next(_request):
            self.fail("canonical redirect must run before authentication")

        response = await app_module.authentication_middleware(Request(scope), unexpected_call_next)
        self.assertEqual(response.status_code, 308)
        self.assertEqual(response.headers["location"], "/history?workflow=minimax-h3&sort=oldest")


if __name__ == "__main__":
    unittest.main()
