(function(){
  const MOBILE_QUERY='(max-width:1099px)';
  const DESKTOP_DIALOGS=new Set([
    'detail-lightbox',
    'media-viewer',
    'chat-attachment-detail',
    'chat-attachment-detail-v3',
    'chat-history-lightbox'
  ]);
  const isMobile=()=>window.matchMedia(MOBILE_QUERY).matches;
  let actionTimer=0;

  function clearActions(except=null){
    document.querySelectorAll('.mobile-media-guard-visible').forEach(node=>{
      if(node!==except)node.classList.remove('mobile-media-guard-visible');
    });
  }

  function addLink(actions,{href,label,download=false}){
    if(!href)return;
    if(download){
      const button=document.createElement('button');
      button.type='button';
      button.className='ghost small';
      button.textContent=label;
      button.addEventListener('click',async event=>{
        event.stopPropagation();
        if(button.disabled)return;
        button.disabled=true;
        const oldLabel=button.textContent;
        button.textContent='正在准备…';
        try{
          if(typeof window.saveCanvasMedia!=='function')throw new Error('保存功能尚未载入，请稍后再试');
          await window.saveCanvasMedia(href,{title:'Comfy Canvas'});
        }catch(error){
          if(typeof window.showActionStatus==='function')window.showActionStatus(error?.message||'保存失败');
          else window.alert(error?.message||'保存失败');
        }finally{
          button.disabled=false;
          button.textContent=oldLabel;
        }
      });
      actions.append(button);
      return;
    }
    const link=document.createElement('a');
    link.className='ghost small';
    link.href=href;
    link.textContent=label;
    link.target='_blank';link.rel='noopener';
    link.addEventListener('click',event=>event.stopPropagation());
    actions.append(link);
  }

  function reveal(host,{src='',downloadUrl='',label='',taskId=''}={}){
    if(!isMobile()||!host)return false;
    clearTimeout(actionTimer);
    clearActions(host);
    host.classList.add('mobile-media-guard-host','mobile-media-guard-visible');
    let actions=host.querySelector(':scope > .mobile-media-guard-actions');
    if(!actions){
      actions=document.createElement('div');
      actions.className='mobile-media-guard-actions';
      host.append(actions);
    }
    actions.replaceChildren();
    addLink(actions,{href:src,label:label||'\u67e5\u770b\u5927\u56fe'});
    addLink(actions,{href:downloadUrl||src,label:'\u4e0b\u8f7d',download:true});
    if(taskId){
      const detail=document.createElement('button');
      detail.type='button';
      detail.className='ghost small';
      detail.dataset.chatOpen=taskId;
      detail.textContent='\u67e5\u770b\u4efb\u52a1\u8be6\u60c5';
      actions.append(detail);
    }
    actionTimer=setTimeout(()=>host.classList.remove('mobile-media-guard-visible'),4000);
    return true;
  }

  function rawImageSource(trigger){
    const image=trigger?.querySelector('img');
    if(!image)return'';
    return String(image.currentSrc||image.src||'')
      .replace(/([?&])preview=\d+(?:&|$)/,'$1')
      .replace(/[?&]$/,'');
  }

  function handleChatTrigger(trigger,event=null){
    if(!isMobile()||!trigger)return false;
    const actions=trigger.closest('.mobile-media-guard-actions');
    if(actions)return false;
    const host=trigger.closest('.chat-attachment');
    const imageSlide=trigger.closest('.chat-inline-media-slide');
    const videoCard=trigger.closest('.chat-inline-video');
    if(!host||(!imageSlide&&!videoCard))return false;
    event?.preventDefault();
    event?.stopImmediatePropagation();
    if(imageSlide){
      const src=rawImageSource(imageSlide);
      return reveal(host,{src,downloadUrl:src,label:'\u67e5\u770b\u5927\u56fe'});
    }
    const video=videoCard.querySelector('video');
    const src=String(video?.currentSrc||video?.src||'');
    return reveal(host,{downloadUrl:src,label:'\u64ad\u653e\u89c6\u9891'});
  }

  function handleDetail(event){
    if(!isMobile()||event.defaultPrevented||event.target.closest('.mobile-media-guard-actions'))return false;
    const sharedDetail=event.target.closest('#view-detail.shared-native-detail');
    if(sharedDetail&&event.target.closest('.source-image,.detail-card a[href],.detail-card img')){
      event.preventDefault();
      event.stopImmediatePropagation();
      return true;
    }
    const source=event.target.closest('#view-detail .source-image');
    if(source){
      event.preventDefault();
      event.stopImmediatePropagation();
      return reveal(source.closest('.source-panel')||source.parentElement,{src:source.href,downloadUrl:source.href,label:'\u67e5\u770b\u539f\u56fe'});
    }
    const link=event.target.closest('#detail-images .detail-card a[href]');
    if(!link)return false;
    const card=link.closest('.detail-card');
    if(card?.classList.contains('mobile-controls-card'))return false;
    event.preventDefault();
    event.stopImmediatePropagation();
    return reveal(card,{src:link.href,downloadUrl:link.href,label:'\u67e5\u770b\u5927\u56fe'});
  }

  function capture(event){
    if(!isMobile())return;
    if(handleDetail(event))return;
    const trigger=event.target.closest('#chat-message-list [data-chat-open]');
    if(trigger)handleChatTrigger(trigger,event);
  }

  // This is the hard boundary: desktop viewers cannot be opened on a mobile viewport.
  const dialogPrototype=window.HTMLDialogElement?.prototype;
  if(dialogPrototype?.showModal){
    const showModal=dialogPrototype.showModal;
    dialogPrototype.showModal=function(...args){
      if(isMobile()&&DESKTOP_DIALOGS.has(this.id))return;
      return showModal.apply(this,args);
    };
  }

  document.addEventListener('click',capture,true);
  document.addEventListener('click',event=>{
    if(!event.target.closest('.mobile-media-guard-actions'))clearActions();
  });

  // Close an already-open desktop viewer when rotating from desktop to mobile.
  window.matchMedia(MOBILE_QUERY).addEventListener('change',event=>{
    if(!event.matches)return;
    DESKTOP_DIALOGS.forEach(id=>{const dialog=document.getElementById(id);if(dialog?.open)dialog.close();});
  });

  window.MobileMediaUI={isMobile,reveal,handleChatTrigger};
})();
