(function(){
  function responseError(body){
    const detail=body?.detail;
    if(Array.isArray(detail))return detail.map(item=>item?.msg||item?.message||JSON.stringify(item)).join('；');
    return typeof detail==='string'?detail:detail?.message||'请求失败';
  }

  function create(options={}){
    const inflightGets=new Map();
    let routeController=null,routeGeneration=0;

    function beginRoute(){
      routeController?.abort();
      routeController=new AbortController();
      routeGeneration+=1;
      return routeController.signal;
    }

    async function perform(url,requestOptions,method){
      const response=await fetch(url,requestOptions);
      if(response.status===401){
        options.onUnauthorized?.();
        throw new Error('登录已过期');
      }
      if(!response.ok){
        const body=await response.json().catch(()=>({}));
        throw new Error(responseError(body));
      }
      const result=response.status===204?null:await response.json();
      if(method==='POST')queueMicrotask(()=>options.onPost?.(url,result));
      return result;
    }

    function request(url,rawOptions={}){
      const {routeScoped=true,...requestOptions}=rawOptions||{};
      const method=String(requestOptions.method||'GET').toUpperCase();
      if(method==='GET'&&routeScoped&&routeController&&!requestOptions.signal)requestOptions.signal=routeController.signal;
      if(method!=='GET')return perform(url,requestOptions,method);
      const generation=routeScoped&&routeController?routeGeneration:0;
      const key=`${generation}:${url}`;
      if(inflightGets.has(key))return inflightGets.get(key);
      const pending=perform(url,requestOptions,method).finally(()=>{
        if(inflightGets.get(key)===pending)inflightGets.delete(key);
      });
      inflightGets.set(key,pending);
      return pending;
    }

    return{request,beginRoute};
  }

  window.ComfyCanvasRequests={create};
})();
