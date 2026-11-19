import {el,html,attr,n,percent,search,control,link,field,select,textArea,check,metrics,table,chart,warning,drawChart} from "./format.js";
import {LAYERS,layerName,libraryPage,rail,capturePanel,resultPanel,resultOverview,readoutPanel,measurementReadout} from "./panels.js";

const ui = {
  stack:null,result:null,capture:null,tab:"library",stackList:[],demos:[],
  capturePanel:"capture",resultPanel:"measure",layer:"composite",tool:"inspect",
  selection:{kind:"none"}, pending:[], marks:[],cards:[],readout:null,override:null,
  showArchived:false,expanded:false,busy:false,deficitKind:"both",deficitMinimum:80,
  plane:null,planeView:"highlight",planeMinimum:24,
  pair:{alignment:"registered",mode:"overlay",amount:.5},recipe:{view:"split",amount:.5},
  schedule:null,paths:{},
};
const app = el("#app"), dialog=el("#dialog");
let noticeTimer, observer, drag=null, modalSubmit=null, measuredRequest=0;

async function request(path,{method="GET",data}={}) {
  const options={method};
  if (data instanceof FormData) options.body=data;
  else if (data!==undefined) {
    options.headers={"Content-Type":"application/json"};options.body=JSON.stringify(data);
  }
  const response=await fetch(path,options);
  if (!response.ok) {
    const problem=response.headers.get("content-type")?.includes("json")?await response.json():{};
    const error=new Error(problem.error??`Request failed (${response.status})`);
    error.details=problem.details;
    throw error;
  }
  return response;
}
async function api(path,options) {
  const response=await request(path,options);
  return response.status===204?null:response.json();
}
async function download(path,data,name) {
  const response=await request(path,{method:"POST",data});
  const blob=await response.blob(), url=URL.createObjectURL(blob);
  const receiver=document.createElement("a");
  receiver.href=url;receiver.download=name;
  receiver.click();
  setTimeout(()=>URL.revokeObjectURL(url),3000);
}

function toast(message,isError=false) {
  const place=el("#notification");
  clearTimeout(noticeTimer);
  place.innerHTML=`<div class="notice ${isError?"error":""}">${html(message)}</div>`;
  noticeTimer=setTimeout(()=>{place.innerHTML="";},isError?6600:4400);
}

function paths() {
  const stack=ui.stack?`/api/stacks/${ui.stack.id}`:"";
  return {
    stack,
    capture:id=>`${stack}/captures/${id}`,
    result:ui.result?`${stack}/results/${ui.result.id}`:"",
  };
}

function updateHash() {
  let hash="";
  if (ui.tab!=="library"&&ui.stack) {
    hash="#"+search({stack:ui.stack.id,result:ui.tab==="result"?ui.result?.id:null});
  }
  if (location.hash!==hash) history.pushState(null,"",location.pathname+location.search+hash);
}

function clearSelection() {
  ui.selection={kind:"none"};ui.pending=[];ui.readout=null;drag=null;
  measuredRequest++;
}

function setProject(project) {
  ui.stack=project;
  if (!project.captures.some(item=>item.id===ui.capture)) {
    ui.capture=project.captures.find(item=>!item.archived)?.id??null;
  }
  ui.paths=paths();
}

async function loadLibrary(update=true) {
  const [list,demos]=await Promise.all([api("/api/stacks"),api("/api/demos")]);
  ui.stackList=list.stacks;ui.demos=demos.demos;ui.tab="library";
  clearSelection();
  if (update) updateHash();
  render();
}

async function loadStack(id,result=null,update=true) {
  ui.tab="capture";ui.capture=null;ui.result=null;ui.override=null;
  ui.capturePanel="capture";ui.resultPanel="measure";ui.tool="inspect";
  ui.showArchived=false;ui.schedule=null;ui.marks=[];ui.cards=[];
  clearSelection();
  setProject(await api(`/api/stacks/${id}`));
  if (result) await loadResult(result,update);
  else {
    if (update) updateHash();
    render();
  }
}

async function loadResult(id,update=true) {
  const base=ui.paths.stack+`/results/${id}`;
  const [result,marks,cards]=await Promise.all([api(base),api(base+"/marks"),api(base+"/cards")]);
  ui.result=result;ui.layer="composite";ui.override=null;
  ui.marks=marks.marks;ui.cards=cards.cards;
  ui.tool="inspect";ui.resultPanel="measure";ui.plane=null;
  ui.pair={alignment:"registered",mode:"overlay",amount:.5};
  ui.recipe={view:"split",amount:.5};
  ui.tab="result";ui.paths=paths();clearSelection();
  if (update) updateHash();
  render();
}

async function followHash() {
  const hash=new URLSearchParams(location.hash.slice(1));
  const stack=hash.get("stack"), result=hash.get("result");
  if (stack&&/^[a-f0-9]{32}$/.test(stack)) return loadStack(stack,result,false);
  return loadLibrary(false);
}

function currentCapture() {
  return ui.stack.captures.find(item=>item.id===ui.capture);
}

function stageInfo() {
  if (ui.override) return ui.override;
  if (ui.tab==="capture") {
    const frame=currentCapture();
    if (!frame) return null;
    return {src:ui.paths.capture(frame.id)+"/preview",label:frame.name,info:frame.width+" × "+frame.height+" input pixels",
      note:n(frame.position,3)+" "+ui.stack.settings.scale.rail_unit,selectable:ui.tool==="area"};
  }
  if (ui.result) {
    const {width,height}=ui.result.summary;
    return {src:ui.paths.result+`/images/${ui.layer}`,label:layerName(ui.layer),info:width+" × "+height+" output pixels",
      note:ui.result.title,selectable:true};
  }
  return null;
}

function stageTools() {
  if (ui.tab==="capture") {
    return `<div class="controls">${control("Capture image","clear-override",{selected:!ui.override})}
      ${control("Select patch","tool",{id:"area",selected:ui.tool==="area",disabled:!ui.capture})}
      ${control(ui.expanded?"Fit view":"Expand view","expand")}</div>`;
  }
  return `<div class="controls layer-controls">${LAYERS.map(([value,label])=>control(label,"layer",{id:value,selected:!ui.override&&ui.layer===value})).join("")}</div>
    <div class="controls tool-controls">
      ${[["inspect","View"],["point","Point"],["area","Area"],["section","Section"]].map(([key,name])=>control(name,"tool",{id:key,selected:ui.tool===key})).join("")}
      <span class="divider"></span>${control("Clear selection","clear-selection")}${control(ui.expanded?"Fit view":"Expand view","expand")}
    </div>`;
}

function render() {
  observer?.disconnect();
  const nav=el("#top-nav");
  if (ui.tab==="library"||!ui.stack) {
    nav.innerHTML="";
    app.innerHTML=libraryPage(ui.stackList,ui.demos);
    return;
  }
  ui.paths=paths();
  nav.innerHTML=control("Captures","show-captures",{selected:ui.tab==="capture"})+
    control("Reconstruction","show-result",{selected:ui.tab==="result",disabled:!ui.stack.results.length});
  const building=ui.busy, output=ui.tab==="result";
  const stage=stageInfo();
  const revision=ui.stack.revision;
  const archived=ui.result&&ui.result.source_revision!==revision;
  app.innerHTML=`<div class="workspace">
    ${rail(ui,ui.paths)}
    <section class="bench" aria-label="Image workbench">
      <div class="bench-head"><div class="workspace-title"><h1>${html(ui.stack.name)}</h1>
        <p class="mono">${ui.stack.demo?"Synthetic target · ":""}Revision ${revision} · ${ui.stack.captures.filter(item=>item.included&&!item.archived).length} included captures</p>
        ${output?`<p class="result-name">${html(ui.result.title)}${archived?" · archived revision "+ui.result.source_revision:""}</p>`:""}</div>
        ${control(building?"Reconstructing…":"Reconstruct","reconstruct",{type:"accent",disabled:building||ui.stack.captures.filter(item=>item.included&&!item.archived).length<3})}
      </div>
      ${stageTools()}
      <div class="stage ${ui.expanded?"fit-expanded":""}" id="stage" aria-label="${attr(stage?.label??"Image display")}">
        ${stage?`<img class="stage-image" id="stage-image" src="${attr(stage.src)}" alt="${attr(stage.label)}">
          <canvas id="selection-canvas" class="${stage.selectable?"interactive":""}"${stage.selectable?' tabindex="0" aria-label="Image selection, use pointer or arrow keys and Enter"':""}></canvas>`
          :`<div class="stage-placeholder"><h2>Build a focus stack</h2><p>Add at least three captures with separate rail positions.</p>${control("Add captures","upload",{type:"accent"})}</div>`}
      </div>
      <div class="stage-caption"><span>${stage?html(stage.label+" · "+stage.info):"Capture view"}</span><span id="stage-help">${html(stageNote())}</span></div>
      <div class="readout" id="readout" aria-live="polite"></div>
    </section>
    ${output?resultPanel(ui):capturePanel(ui)}
  </div>`;
  drawReadout();
  const image=el("#stage-image"), pane=el("#stage");
  if (image) {
    image.addEventListener("load",drawSelection,{once:true});
    image.addEventListener("error",()=>toast("The image layer could not be opened.",true),{once:true});
    observer=new ResizeObserver(()=>{drawSelection();drawReadoutChart();});
    observer.observe(pane);
    if (image.complete) drawSelection();
  }
}
function stageNote() {
  if (ui.override) return ui.override.note??"";
  if (ui.tool==="area") return "Drag a rectangle inside the image.";
  if (ui.tool==="point") return "Click an image point to read its focus response.";
  if (ui.tool==="section") return ui.pending.length?"Click the second endpoint.":"Click two endpoints to measure a depth section.";
  if (ui.tool==="polygon") return ui.pending.length+` vertices · Finish polygon in Notebook.`;
  return stageInfo()?.note??"";
}
function refreshSide() {
  const element=el(".right-pane");
  if (element&&ui.tab==="result") element.outerHTML=resultPanel(ui);
  else if (element&&ui.tab==="capture") element.outerHTML=capturePanel(ui);
  const note=el("#stage-help");
  if (note) note.textContent=stageNote();
}
function drawReadout() {
  const content=el("#readout");
  if (!content) return;
  if (ui.readout) content.innerHTML=readoutPanel(ui.readout[0],ui.readout[1]);
  else if (ui.tab==="result"&&ui.result) content.innerHTML=readoutPanel("Result overview",resultOverview(ui.result.summary));
  else content.innerHTML="";
  drawReadoutChart();
}
function drawReadoutChart() {
  drawChart(el("#readout #chart"),ui.readout?.[2]);
}
function showReadout(title,body,plot=null) {
  ui.readout=[title,body,plot];drawReadout();
}
function showDisplay(src,label,info,note="") {
  ui.override={src,label,info,note,selectable:false};render();
}

function pointInImage(clientX,clientY,clamp=false) {
  const image=el("#stage-image");
  if (!image?.complete||!image.naturalWidth) return null;
  const b=image.getBoundingClientRect();
  const x=(clientX-b.left)/b.width, y=(clientY-b.top)/b.height;
  if (!clamp&&(x<0||x>1||y<0||y>1)) return null;
  return [Math.max(0,Math.min(1,x)),Math.max(0,Math.min(1,y))];
}

function drawSelection() {
  const canvas=el("#selection-canvas"), image=el("#stage-image");
  if (!canvas||!image||!image.naturalWidth) return;
  const b=canvas.getBoundingClientRect(), r=image.getBoundingClientRect(), dpi=Math.min(devicePixelRatio||1,2);
  canvas.width=Math.round(b.width*dpi);canvas.height=Math.round(b.height*dpi);
  if (!stageInfo()?.selectable&&ui.override) return;
  const context=canvas.getContext("2d");
  context.scale(dpi,dpi);
  const coords=([x,y])=>[r.left-b.left+x*r.width,r.top-b.top+y*r.height];
  const styles={amber:"#ffc76b",cyan:"#89e5ea",magenta:"#eea8e6",white:"#ecf0f7"};
  function markup(points,kind,color,label="") {
    if (!points.length) return;
    const vertices=points.map(coords);
    context.lineWidth=1.6;context.strokeStyle=color;context.fillStyle=color;
    if (vertices.length===1&&kind!=="polygon") {
      const [x,y]=vertices[0];
      context.beginPath();context.arc(x,y,6,0,Math.PI*2);context.moveTo(x-12,y);context.lineTo(x+12,y);context.moveTo(x,y-12);context.lineTo(x,y+12);context.stroke();
    } else {
      context.beginPath();
      vertices.forEach(([x,y],i)=>i?context.lineTo(x,y):context.moveTo(x,y));
      if (kind==="area"||kind==="polygon") context.closePath();
      context.stroke();
      for (const [x,y] of vertices) { context.beginPath();context.arc(x,y,3,0,Math.PI*2);context.fill(); }
    }
    if (label) {
      const [x,y]=vertices[0];
      context.font="12px system-ui";context.textBaseline="bottom";
      const size=context.measureText(label).width;
      context.fillStyle="#171b22dd";context.fillRect(x+6,y-20,size+9,15);
      context.fillStyle=color;context.fillText(label,x+10,y-7);
    }
  }
  const rectangle=region=>[[region.left,region.top],[region.right,region.top],[region.right,region.bottom],[region.left,region.bottom]];
  for (const mark of ui.tab==="result"?ui.marks:[]) markup(mark.vertices,mark.kind,styles[mark.color],mark.label);
  const selection=ui.selection;
  if (selection.kind==="pin") markup([selection.point],"pin",styles.amber);
  if (selection.kind==="area") markup(rectangle(selection.region),"area",styles.amber);
  if (selection.kind==="section"||selection.kind==="polygon") markup(selection.points,selection.kind,styles.amber);
  if (ui.pending.length) markup(ui.pending,ui.tool==="polygon"?"polygon":"section",styles.cyan);
  if (drag) markup(rectangle(bounds(drag.start,drag.end)),"area",styles.amber);
  if (canvas===document.activeElement&&ui.cursor) markup([ui.cursor],"pin",styles.cyan);
}

function bounds(a,b) {
  return {left:Math.min(a[0],b[0]),top:Math.min(a[1],b[1]),right:Math.max(a[0],b[0]),bottom:Math.max(a[1],b[1])};
}

async function measure(selection) {
  const serial=++measuredRequest;
  ui.selection=selection;ui.pending=[];ui.readout=null;
  drawSelection();refreshSide();
  if (ui.tab==="capture") {
    showReadout("Selected capture patch",`<p>Run the exposure histogram to inspect the selected pixels.</p>${control("Exposure histogram","exposure")}`);
    return;
  }
  const path=ui.paths.result;
  try {
    let result;
    if (selection.kind==="pin") result=await api(path+"/point",{method:"POST",data:{point:selection.point}});
    if (selection.kind==="area") result=await api(path+"/area",{method:"POST",data:{region:selection.region}});
    if (selection.kind==="section") result=await api(path+"/section",{method:"POST",data:{points:selection.points,samples:selection.samples??80}});
    if (serial!==measuredRequest||!result) return;
    const reading=measurementReadout(selection,result);
    showReadout(...reading);
  } catch (error) {
    if (serial===measuredRequest) toast(error.message,true);
  }
}

function handlePoint(point) {
  if (ui.tool==="point") return measure({kind:"pin",point});
  if (ui.tool==="section") {
    if (!ui.pending.length) {
      ui.pending=[point];drawSelection();refreshSide();return;
    }
    return measure({kind:"section",points:[ui.pending[0],point],samples:80});
  }
  if (ui.tool==="polygon"&&ui.pending.length<24) {
    ui.pending.push(point);drawSelection();refreshSide();
  }
}

app.addEventListener("pointerdown",event=>{
  if (event.target.id!=="selection-canvas"||event.button!==0||ui.override||ui.tool==="inspect") return;
  const point=pointInImage(event.clientX,event.clientY);
  if (!point) return;
  event.preventDefault();
  if (ui.tool==="area") {
    drag={start:point,end:point};event.target.setPointerCapture(event.pointerId);drawSelection();
  } else handlePoint(point);
});
app.addEventListener("pointermove",event=>{
  if (!drag||event.target.id!=="selection-canvas") return;
  drag.end=pointInImage(event.clientX,event.clientY,true)??drag.end;drawSelection();
});
app.addEventListener("pointercancel",()=>{
  if (drag) {drag=null;drawSelection();}
});
app.addEventListener("pointerup",event=>{
  if (!drag||event.target.id!=="selection-canvas") return;
  const end=pointInImage(event.clientX,event.clientY,true)??drag.end;
  const region=bounds(drag.start,end);drag=null;
  if (region.right-region.left>=.005&&region.bottom-region.top>=.005) measure({kind:"area",region,bins:18});
  else {drawSelection();toast("Drag a wider and taller rectangle.");}
});
app.addEventListener("keydown",event=>{
  if (event.target.id!=="selection-canvas") return;
  const deltas={ArrowLeft:[-.01,0],ArrowRight:[.01,0],ArrowUp:[0,-.01],ArrowDown:[0,.01]};
  if (deltas[event.key]) {
    event.preventDefault();
    ui.cursor=(ui.cursor??[.5,.5]).map((coord,i)=>Math.max(0,Math.min(1,coord+deltas[event.key][i])));
    drawSelection();
  } else if (event.key==="Enter") {
    event.preventDefault();handlePoint(ui.cursor??[.5,.5]);
  } else if (event.key==="Escape") {
    clearSelection();drawSelection();drawReadout();refreshSide();
  }
});
app.addEventListener("focusout",event=>{
  if (event.target.id==="selection-canvas") setTimeout(drawSelection,1);
});

function modal(title,body,onSubmit,{label="Save",extra=""}={}) {
  el("#dialog-title").textContent=title;
  el("#dialog-body").innerHTML=body;
  el("#dialog-message").textContent="";
  el("#dialog-actions").innerHTML=control("Cancel","close-dialog")+
    extra+(onSubmit?`<button type="submit" class="accent">${html(label)}</button>`:"");
  modalSubmit=onSubmit;
  if (!dialog.open) dialog.showModal();
}
function modalError(error) {
  el("#dialog-message").textContent=error.message;
}
async function commitDialog(event) {
  event.preventDefault();
  if (!modalSubmit) return;
  const form=event.currentTarget, action=event.submitter;
  if (action) action.disabled=true;
  el("#dialog-message").textContent="";
  try {
    const result=await modalSubmit(new FormData(form),action?.value);
    if (result!==false&&dialog.open) dialog.close();
  } catch (error) {
    modalError(error);
  } finally { if (action) action.disabled=false; }
}
el("#dialog-form").addEventListener("submit",commitDialog);
function replaceProject(project,showNote) {
  setProject(project);render();
  if (showNote) toast(showNote);
}
async function updateProject(data) {
  const project=await api(ui.paths.stack,{method:"PATCH",data:{revision:ui.stack.revision,...data}});
  replaceProject(project);
  return project;
}
function fillRunDefaults(form) {
  const mode=form.get("rail_mode")??"linear";
  const scale=ui.stack.settings.scale;
  const host=document.querySelector("#rail-pairs");
  const pairs=host?Array.from(host.querySelectorAll(".field-row")).map(row=>({
    step:row.querySelector('input[name="step"]')?.value,um:row.querySelector('input[name="um"]')?.value,
  })).filter(row=>row.step!==""||row.um!==""):[];
  return {
    registration:form.get("registration"),reference_id:form.get("reference_id")||null,score:form.get("score"),
    blend:form.get("blend"),viewport:form.get("viewport"),
    confidence_floor:form.get("confidence_floor"),edge_guard:form.get("edge_guard"),strength:form.get("strength"),
    scale:{rail_unit:form.get("rail_unit")??scale.rail_unit,rail_offset:form.get("rail_offset")??scale.rail_offset,
      pixel_um:form.get("pixel_um")||null,rail_mode:mode,calibration:mode==="calibration"?{points:pairs}:scale.calibration},
  };
}
function currentRegion() {
  return ui.selection.kind==="area"?ui.selection.region:null;
}
function queryRegion(region) {
  return region??{};
}
function projectForm(title,current,onSubmit,label) {
  modal(title,field("Stack name","name",current,{required:true}),form=>onSubmit(form.get("name")),{label});
}

function uploadDialog(replacement=false) {
  const limit=replacement?1:16, target=currentCapture();
  const instructions=replacement?"The previous capture stays in the archive at its current rail position.":"Assign a separate rail position to each capture, or import a rail log afterward.";
  const fields=`<p>${html(instructions)}</p><label class="field">${replacement?"Replacement image":"Capture images"}
    <input type="file" name="images" accept=".png,.jpg,.jpeg,.tif,.tiff,.webp" ${replacement?"":"multiple"}></label><div id="upload-positions"></div>`;
  modal(replacement?"Retake capture":"Add captures",fields,async form=>{
    const files=form.getAll("images").filter(file=>file.size);
    if (files.length<1||files.length>limit) throw new Error(`Choose ${replacement?"one image":"1–16 images"}.`);
    const content=new FormData();
    content.set("revision",ui.stack.revision);
    let path=ui.paths.stack+"/captures";
    if (replacement) {
      path=ui.paths.capture(target.id)+"/retake";content.set("image",files[0]);
    } else {
      files.forEach(file=>content.append("images",file));form.getAll("positions").forEach(value=>content.append("positions",value));
    }
    const updated=await api(path,{method:"POST",data:content});
    ui.override=null;clearSelection();
    const current=replacement?updated.captures.find(item=>item.slot===target.slot&&!item.archived):updated.captures.filter(item=>!item.archived).at(-files.length);
    ui.capture=current?.id??null;
    replaceProject(updated,replacement?"Replacement saved. The old capture is archived.":`${files.length} captures added.`);
  },{label:replacement?"Retake":"Add captures"});
  if (!replacement) el('#dialog input[type="file"]').addEventListener("change",event=>{
    const start=Math.max(-1,...ui.stack.captures.filter(item=>!item.archived).map(item=>item.position))+1;
    const files=Array.from(event.target.files);
    el("#upload-positions").innerHTML=files.slice(0,limit).map((file,i)=>field(file.name,"positions",start+i,{type:"number",step:"any",required:true})).join("");
  });
}

function railLogDialog() {
  let staged=null;
  const body=field("Rail log CSV","log","",{type:"file"})+
    `<p class="muted">Use a filename,position header. An optional unit column accepts µm, mm or step.</p>${link("Current positions CSV",ui.paths.stack+"/capture-log.csv")}<div id="log-report"></div>`;
  modal("Attach a measured rail log",body,async (form,action)=>{
    if (!form.get("log")?.size) throw new Error("Choose a CSV rail log.");
    if (action==="apply") {
      if (!staged?.valid) throw new Error("Preview a matching rail log before applying.");
      form.set("mode","apply");form.set("revision",staged.stack_revision);
      const result=await api(ui.paths.stack+"/positions-log",{method:"POST",data:form});
      replaceProject(result.stack,"Rail positions updated.");return true;
    }
    form.set("mode","preview");
    staged=await api(ui.paths.stack+"/positions-log",{method:"POST",data:form});
    el("#log-report").innerHTML=table(["File","Previous","New","Status"],staged.rows.map(item=>[item.filename,n(item.old_position),n(item.new_position),item.status]))+
      staged.collisions.map(info=>warning(`Position ${n(info.position)} matches ${info.captures.join(", ")}.`)).join("")+
      (staged.valid?`<p class="success">Every row is matched. Apply to update the stack.</p>`:"");
    if (staged.valid&&!el('#dialog button[value="apply"]')) {
      el("#dialog-actions").insertAdjacentHTML("beforeend",'<button type="submit" class="accent" value="apply">Apply positions</button>');
    }
    const apply=el('#dialog button[value="apply"]');
    if (apply) apply.disabled=!staged.valid;
    return false;
  },{label:"Preview log"});
  el('#dialog input[type="file"]').addEventListener("change",()=>{
    staged=null;el("#log-report").innerHTML="";
    el('#dialog button[value="apply"]')?.remove();
  });
}

function planForm() {
  const positions=ui.stack.captures.filter(frame=>frame.included&&!frame.archived).map(frame=>frame.position);
  const suggested=ui.schedule??{first:positions.length?Math.min(...positions):0,last:positions.length>1?Math.max(...positions):140,
    in_focus_width:34,overlap_percent:40,usable_frames:24,settle_ms:300,interval_ms:200};
  const unit=ui.stack.settings.scale.rail_unit;
  const count={type:"number",step:1};
  const distance={type:"number",step:"any"};
  const body=`<div class="stack"><div class="field-row">${field("First position ("+unit+")","first",suggested.first,distance)}${field("Last position ("+unit+")","last",suggested.last,distance)}</div>
    <div class="field-row">${field("Observed sharp width","in_focus_width",suggested.in_focus_width,distance)}${field("Desired overlap (%)","overlap_percent",suggested.overlap_percent,count)}</div>
    <div class="field-row three-fields">${field("Frame budget","usable_frames",suggested.usable_frames,count)}${field("Settle (ms)","settle_ms",suggested.settle_ms,count)}${field("Capture (ms)","interval_ms",suggested.interval_ms,count)}</div>
    <div id="schedule-preview"></div></div>`;
  modal("Plan a focus sweep",body,async(form,action)=>{
    const plan=Object.fromEntries(form);
    if (action==="schedule-csv") {
      await download(ui.paths.stack+"/schedule",{plan,download:true},"focalbench-rail-schedule.csv");return false;
    }
    const result=await api(ui.paths.stack+"/schedule",{method:"POST",data:{plan}});
    ui.schedule=plan;
    const print=result.schedule;
    el("#schedule-preview").innerHTML=metrics([["Frames",print.frames+"/"+print.required_frames],["Step",n(print.actual_step,3)+" "+unit],["Overlap",n(print.overlap_percent,1)+"%"],["Capture time",n(print.estimated_ms/1000,1)+" s"]])+
      result.warnings.map(warning).join("")+table(["Order","Target","Recorded","Offset"],result.matches.targets.map(row=>[row.index,n(row.target,3),n(row.recorded,3),n(row.offset,3)]));
    if (!el('#dialog button[value="schedule-csv"]')) {
      el("#dialog-actions").insertAdjacentHTML("beforeend",'<button type="submit" value="schedule-csv">Schedule CSV</button>');
    }
    return false;
  },{label:"Calculate schedule"});
}

async function checkPreflight() {
  toast("Reading the capture files…");
  const result=await api(ui.paths.stack+"/preflight");
  const rows=result.captures.map(item=>[item.name,n(item.position,3),n(item.brightness.median,3),percent(item.overexposed),item.flags.map(flag=>flag.detail).join(" · ")||"Ready"]);
  modal("Capture preflight",`<p class="${result.can_reconstruct?"success":"error"}">${result.can_reconstruct?"Ready to reconstruct":"Resolve the blockers before reconstructing"}</p>
    ${result.blockers.map(blocker=>warning(blocker.detail)).join("")}
    ${result.rail.gaps.map(gap=>warning(`Rail gap ${n(gap.start)} to ${n(gap.end)}.`)).join("")}
    ${table(["Capture","Rail","Median light","Clipped","Observation"],rows)}`,null);
}

async function exposureReadout() {
  const frame=currentCapture(), region=currentRegion(), base=ui.paths.capture(frame.id);
  const data=await api(base+"/radiometry",{method:"POST",data:{region}});
  showDisplay(base+"/radiometry.png?"+search({...queryRegion(region),mode:"both"}),"Exposure map","Before alignment","Color marks clipped channels and deep shadows");
  const config={
    ymin:0,xmin:0,xmax:1,
    series:data.channels.map((row,i)=>({color:["#ee8586","#8cdeb1","#a1bfff"][i],points:row.bins.map((count,index)=>[(index+.5)/64,count])})),
  };
  showReadout("Exposure histogram",metrics([["Visible samples",n(data.covered_pixels,0)],["Median luminance",n(data.luminance.median,3)],["Clipped",n(data.clipping_pixels,0)],["Blocked shadows",n(data.blocked_shadows,0)]])+chart("Linear red, green and blue signal histograms"),config);
}

async function inspectSharp() {
  const region=currentRegion();
  const data=await api(ui.paths.result+"/sharp-interval",{method:"POST",data:{region}});
  const interval=data.strongest, canUse=data.safe_width_for_plan!==null;
  const content=metrics([["Peak position",n(interval.peak_position)+" "+data.unit],
    ["Lower crossing",n(interval.low)+" "+data.unit],["Upper crossing",n(interval.high)+" "+data.unit],
    ["Measured sharp width",n(interval.width)+" "+data.unit]])+
    data.warnings.map(warning).join("")+chart("Sharpness response and selected threshold")+
    (canUse?`<div class="row">${control("Use width in schedule","send-width",{id:data.safe_width_for_plan})}</div>`:"");
  showReadout("Observed sharp interval",content,{unit:data.unit,ymin:0,guide:data.response_gate,series:[{points:data.curve.map(row=>[row.position,row.response])}]});
}

async function patchDialog() {
  const region=currentRegion(), base=ui.paths.result+"/patch";
  const data=await api(base,{method:"POST",data:{region}});
  const body=metrics([["Selected pixels",n(data.pixels,0)],["Physical area",n(data.square_um)+" µm²"]])+
    `<p class="muted">Includes native-size composite, focus maps and a float depth TIFF with coordinates.</p>`+
    (data.pixel_csv_allowed?check("Include per-pixel CSV","csv",true):warning(`Per-pixel CSV requires at most ${n(data.pixel_csv_limit,0)} pixels.`));
  modal("Export selected patch",body,form=>download(base,{region,download:true,csv:form.has("csv")},"focalbench-patch.zip"),{label:"Download ZIP"});
}

function datumDialog() {
  const region=currentRegion(), base=ui.paths.result+"/datum";
  const unit=ui.result.summary.scale.rail_unit;
  const body=field("Assign selected surface this reading ("+unit+")","position",0,{type:"number",step:"any"})+
    `<p class="muted">This origin applies to new results. Existing measurements stay attached to their archived origin.</p><div id="datum-report"></div>`;
  modal("Set a rail datum",body,async(form,action)=>{
    const position=form.get("position"), apply=action==="apply-datum";
    const data=await api(base,{method:"POST",data:{region,position,apply,revision:ui.stack.revision}});
    if (apply) {replaceProject(data.stack,"The next reconstruction will use the new rail origin.");return true;}
    el("#datum-report").innerHTML=data.ready
      ?metrics([["Current median",n(data.measured_median)+" "+unit],["New origin",n(data.recommended_origin)+" "+unit],["Reliable",percent(data.reliable_fraction)]])+(data.warning?warning(data.warning):"")
      :warning(data.reason);
    if (data.ready&&data.inside_supported_limits&&!el('#dialog button[value="apply-datum"]')) {
      el("#dialog-actions").insertAdjacentHTML("beforeend",'<button type="submit" value="apply-datum" class="accent">Apply new origin</button>');
    }
    return false;
  },{label:"Preview datum"});
}

function calibrationDialog() {
  const points=ui.selection.points, base=ui.paths.result+"/calibration";
  const body=`<div class="field-row">${field("Known distance","length","",{type:"number",step:"any",required:true})}
    ${select("Known unit","unit",[["µm","µm"],["mm","mm"]],"µm")}</div>
    ${field("Tolerance (in known unit)","tolerance",0,{type:"number",step:"any"})}
    <div id="calibration-report"></div>`;
  modal("Calibrate reference pixel size",body,async(form,action)=>{
    const apply=action==="apply-scale";
    const data=await api(base,{method:"POST",data:{spans:[{points,length:form.get("length"),unit:form.get("unit"),tolerance:form.get("tolerance")}],apply,revision:ui.stack.revision}});
    if (apply) {replaceProject(data.stack,"Pixel scale saved for future reconstructions.");return true;}
    el("#calibration-report").innerHTML=metrics([["Pixel size",n(data.value,5)+" µm / px"],["Estimated uncertainty",n(data.uncertainty,5)],["Original span",n(data.spans[0].original_distance_px,2)+" px"]])+data.warnings.map(warning).join("");
    if (data.can_apply&&!el('#dialog button[value="apply-scale"]')) {
      el("#dialog-actions").insertAdjacentHTML("beforeend",'<button type="submit" value="apply-scale" class="accent">Apply scale</button>');
    }
    return false;
  },{label:"Measure scale"});
}

function markDialog() {
  const selection=ui.selection;
  let kind=selection.kind, vertices;
  if (kind==="pin") vertices=[selection.point];
  if (kind==="section") {kind="line";vertices=selection.points;}
  if (kind==="area") {
    const r=selection.region;
    vertices=[[r.left,r.top],[r.right,r.top],[r.right,r.bottom],[r.left,r.bottom]];
  }
  if (kind==="polygon") {kind="area";vertices=selection.points;}
  const body=`<div class="stack">${field("Annotation label","label","",{required:true})}
    ${select("Color","color",[["amber","Amber"],["cyan","Cyan"],["magenta","Magenta"],["white","White"]],"amber")}
    ${textArea("Note","note")}</div>`;
  modal("Annotate the selected image",body,async form=>{
    const output=await api(ui.paths.result+"/marks",{method:"POST",data:{kind,vertices,...Object.fromEntries(form)}});
    ui.marks.push(output);clearSelection();render();toast("Annotation saved.");
  },{label:"Save annotation"});
}
function saveCard() {
  const body=`<div class="stack">${field("Inspection title","title","",{required:true})}${textArea("Inspection note","note")}</div>`;
  modal("Save this inspection",body,async form=>{
    const data={...Object.fromEntries(form),layer:ui.layer,selection:ui.selection};
    const card=await api(ui.paths.result+"/cards",{method:"POST",data});
    ui.cards.unshift(card);refreshSide();toast("Inspection saved.");
  });
}

async function openCard(id) {
  const card=ui.cards.find(item=>item.id===id);
  if (!card) return;
  ui.override=null;ui.layer=card.content.layer;ui.selection=card.content.selection;
  ui.pending=[];ui.tool="inspect";ui.readout=null;render();
  const data=card.content.measurement;
  if (data) {
    const reading=measurementReadout(ui.selection,data);
    showReadout(card.title,`<p class="muted">${html(card.content.note)}</p>`+reading[1],reading[2]);
  } else showReadout(card.title,`<p>${html(card.content.note||layerName(ui.layer))}</p>`);
}

const buttonActions={
  "show-library":()=>loadLibrary(),
  "new-stack":()=>projectForm("Create a focus stack","",async name=>{
    const project=await api("/api/stacks",{method:"POST",data:{name}});
    await loadStack(project.id);
  },"Create stack"),
  "open-stack":id=>loadStack(id),
  "demo":async id=>{
    toast("Preparing synthetic capture planes…");
    const project=await api("/api/stacks/demo",{method:"POST",data:{demo:id}});
    await loadStack(project.id);
  },
  "import-zip":()=>modal("Import a portable stack",`<div class="stack">${field("Capture ZIP","archive","",{type:"file"})}${field("Stack name (optional)","name")}</div>`,async form=>{
    if (!form.get("archive")?.size) throw new Error("Choose a portable capture ZIP.");
    const project=await api("/api/stacks/import",{method:"POST",data:form});
    await loadStack(project.id);
  },{label:"Import stack"}),
  "show-captures":()=>{
    ui.tab="capture";ui.tool="inspect";ui.override=null;clearSelection();updateHash();render();
  },
  "show-result":()=>{
    const chosen=ui.result?.id??ui.stack.results[0]?.id;
    if (chosen) return loadResult(chosen);
  },
  "open-result":id=>loadResult(id),
  "select-capture":id=>{
    ui.capture=id;ui.tab="capture";ui.capturePanel="capture";
    ui.tool="inspect";ui.override=null;clearSelection();updateHash();render();
  },
  "toggle-archived":()=>{ui.showArchived=!ui.showArchived;render();},
  "capture-panel":id=>{ui.capturePanel=id;render();},
  "result-panel":id=>{ui.resultPanel=id;refreshSide();},
  "layer":id=>{ui.layer=id;ui.override=null;render();},
  "tool":id=>{
    ui.tool=id;ui.override=null;ui.pending=[];ui.cursor=[.5,.5];render();
  },
  "finish-polygon":()=>{
    if (ui.pending.length<3) return;
    ui.selection={kind:"polygon",points:ui.pending.slice()};ui.pending=[];ui.readout=null;ui.tool="inspect";render();
    showReadout("Polygon selection",`<p>${ui.selection.points.length} vertices. Save it with Annotate selection in Notebook.</p>`);
  },
  "clear-selection":()=>{clearSelection();drawSelection();drawReadout();refreshSide();},
  "clear-override":()=>{ui.override=null;ui.readout=null;render();},
  "expand":()=>{ui.expanded=!ui.expanded;render();},
  "stack-meta":()=>modal("Stack name and notes",`<div class="stack">${field("Name","name",ui.stack.name,{required:true})}${textArea("Stack note","note",ui.stack.note)}</div>`,async form=>{
    await updateProject(Object.fromEntries(form));toast("Stack details saved.");
  }),
  "add-rail-pair":()=>{
    const host=el("#rail-pairs");
    if (!host) return;
    const blank=host.querySelector(".empty-pair");
    if (blank) blank.remove();
    host.insertAdjacentHTML("beforeend",`<div class="field-row three-fields">
      <label class="field">Stage step<input type="number" step="any" name="step" value=""></label>
      <label class="field">Height (µm)<input type="number" step="any" name="um" value=""></label>
      <button type="button" class="quiet" data-action="remove-rail-pair">Remove</button></div>`);
    host.querySelector(".field-row:last-child input")?.focus();
  },
  "remove-rail-pair":(_,button)=>{
    const row=button?.closest?.(".field-row");
    if (row) row.remove();
  },
  "fork-stack":()=>projectForm("Fork the current stack",ui.stack.name+" copy",async name=>{
    const newStack=await api(ui.paths.stack+"/fork",{method:"POST",data:{name}});await loadStack(newStack.id);
  },"Fork stack"),
  "fork-result":()=>projectForm("Fork from the archived captures",ui.stack.name+" from result",async name=>{
    const newStack=await api(ui.paths.result+"/fork",{method:"POST",data:{name}});await loadStack(newStack.id);
  },"Fork result"),
  "upload":()=>uploadDialog(),
  "retake":()=>uploadDialog(true),
  "rail-log":railLogDialog,
  "schedule":planForm,
  "preflight":checkPreflight,
  "contact-sheet":()=>{
    const suffix=ui.showArchived?"?archived=1":"";
    showDisplay(ui.paths.stack+"/contact.png"+suffix,"Capture contact sheet","Raw source proof","Open the image or download the proof");
    showReadout("Capture contact sheet",link("Contact sheet PNG",ui.paths.stack+"/contact.png?download=1"+(ui.showArchived?"&archived=1":"")));
  },
  "exposure":exposureReadout,
  "reconstruct":async ()=>{
    if (ui.busy) return;
    ui.busy=true;render();toast("Aligning captures and computing focus evidence…");
    try {
      const output=await api(ui.paths.stack+"/results",{method:"POST",data:{revision:ui.stack.revision,name:`Focus reconstruction ${ui.stack.results.length+1}`}});
      setProject(await api(ui.paths.stack));
      ui.busy=false;await loadResult(output.id);toast("Reconstruction archived.");
    } finally {if (ui.busy) {ui.busy=false;render();}}
  },
  "sharp-width":inspectSharp,
  "send-width":id=>{
    const summary=ui.result.summary, frames=summary.captures;
    const steps=frames.map(row=>row.position);
    const unit=summary.surface_unit;
    const calibrated=summary.depth_encoding.calibrated;
    // The schedule plan is in capture rail coordinates. A calibrated µm width
    // only converts back to steps when the marks share one ratio.
    let width=Number(id);
    if (calibrated) {
      const pairs=summary.rail_calibration?.points??[];
      const ratios=pairs.slice(1).map((row,i)=>(row.um-pairs[i].um)/(row.step-pairs[i].step));
      const spread=ratios.length?Math.max(...ratios)-Math.min(...ratios):Infinity;
      if (ratios.length&&spread<=Math.max(...ratios)*0.02) width=width/ratios[0];
      else {
        toast(`This rail mixes ${ratios.length||"different"} measured spans, so a ${unit} sharp width reads differently at each step. Enter the plan in stage steps instead.`,true);
        planForm();
        return;
      }
    }
    ui.schedule={first:Math.min(...steps),last:Math.max(...steps),
      in_focus_width:width,overlap_percent:40,usable_frames:24,settle_ms:300,interval_ms:200};
    planForm();
  },
  "patch":patchDialog,
  "datum":datumDialog,
  "calibrate":calibrationDialog,
  "section-csv":()=>download(ui.paths.result+"/section",{points:ui.selection.points,samples:ui.selection.samples??80,download:true},"focalbench-depth-section.csv"),
  "mark":markDialog,
  "save-card":saveCard,
  "open-card":openCard,
  "delete-mark":id=>{
    const label=ui.marks.find(mark=>mark.id===id)?.label;
    modal("Remove annotation",`<p>Remove ${html(label)} from this reconstruction?</p>`,async ()=>{
      await api(ui.paths.result+"/marks/"+id,{method:"DELETE"});
      ui.marks=ui.marks.filter(item=>item.id!==id);refreshSide();drawSelection();
    },{label:"Remove"});
  },
  "delete-card":id=>{
    const label=ui.cards.find(card=>card.id===id)?.title;
    modal("Remove inspection",`<p>Remove ${html(label)} from the notebook?</p>`,async ()=>{
      await api(ui.paths.result+"/cards/"+id,{method:"DELETE"});
      ui.cards=ui.cards.filter(item=>item.id!==id);refreshSide();
    },{label:"Remove"});
  },
  "integrity":async ()=>{
    const data=await api(ui.paths.result+"/source-integrity");
    modal("Source file integrity",`<p class="${data.all_sources_match?"success":"error"}">${data.all_sources_match?"All archived source hashes match.":"Some source bytes are missing or have changed."}</p>`+
      table(["Capture","State","Original digest"],data.checks.map(row=>[row.name,row.state,row.expected_sha256])),null);
  },
  "close-dialog":()=>dialog.close(),
};

async function editCapture(form) {
  const frame=currentCapture();
  const data={position:form.get("position"),enabled:form.has("enabled"),note:form.get("note"),revision:ui.stack.revision};
  const project=await api(ui.paths.capture(frame.id),{method:"PATCH",data});
  replaceProject(project,"Capture updated.");
}

async function findDeficits(form) {
  const region=form.has("scoped")?currentRegion():null;
  if (form.has("scoped")&&!region) throw new Error("Drag a rectangle before limiting to a selection.");
  const kind=form.get("kind"), minimum=form.get("minimum");
  const result=await api(ui.paths.result+"/deficits",{method:"POST",data:{kind,minimum,region}});
  ui.deficitKind=kind;ui.deficitMinimum=minimum;
  showDisplay(ui.paths.result+"/deficits.png?"+search({kind,minimum,...queryRegion(region)}),"Coverage gaps","Archived result","Numbers identify the regions below");
  showReadout("Focus coverage gaps",metrics([["Candidate pixels",n(result.selected_pixels,0)],["Regions above minimum",n(result.kept_regions,0)],["Pixels in those regions",n(result.kept_pixels,0)]])+
    table(["Region","Pixels","Touches edge","Observation"],result.components.map((row,i)=>[i+1,n(row.pixels,0),row.touches_output_edge?"Yes":"No",row.reason])));
}

async function showPlane(form) {
  const capture=form.get("capture"), view=form.get("view"), minimum=form.get("minimum");
  const base=ui.paths.result+"/planes/"+capture;
  const details=await api(base+"?"+search({minimum}));
  ui.plane=capture;ui.planeView=view;ui.planeMinimum=minimum;
  showDisplay(base+"/image?"+search({view}),"Source contribution",details.capture.name,n(details.capture.position)+" "+details.capture.unit);
  showReadout("Reliable focus contribution",metrics([["Eligible",n(details.eligible_pixels,0)],["Reliable winners",n(details.reliable_winner_pixels,0)],
    ["Output fraction",percent(details.reliable_fraction)],["Focus islands",n(details.distinct_islands,0)]])+
    table(["Island","Pixels","Bounds"],details.returned_islands.map(row=>[row.rank,n(row.pixels,0),`${row.x}, ${row.y} · ${row.width} × ${row.height}`]))+
    link("Plane display PNG",base+"/image?"+search({view,download:1})));
}

async function showPair(form) {
  const pairs=Object.fromEntries(form), region=currentRegion();
  const data={first:pairs.first,second:pairs.second,registered:pairs.alignment==="registered",region};
  const report=await api(ui.paths.result+"/pair",{method:"POST",data});
  ui.pair=pairs;
  const first=report.first.name, second=report.second.name;
  showDisplay(ui.paths.result+"/pair.png?"+search(pairs),"Capture pair",pairs.alignment==="registered"?"Aligned capture positions":"Before alignment",first+" / "+second);
  showReadout("Capture pair comparison",metrics([["Shared coverage",percent(report.coverage)],["Mean light difference",n(report.mean_difference,4)],
    ["Detail correlation",n(report.detail_correlation,4)],["Rail positions",n(report.first.position)+" / "+n(report.second.position)+" "+report.unit]]));
}

async function showRecipe(form) {
  const recipe=Object.fromEntries(form);
  const output=await api(ui.paths.result+"/variants",{method:"POST",data:{other:recipe.other}});
  ui.recipe=recipe;
  showDisplay(ui.paths.result+"/variants.png?"+search(recipe),"Reconstruction comparison","Common reference coordinates",output.reference_name);
  showReadout("Recipe comparison",metrics([["Shared reliable area",percent(output.coverage.jointly_reliable)],
    ["Median absolute depth change",n(output.median_absolute_difference,3)+" "+output.position_unit],
    ["P95 change",n(output.p95_absolute_difference,3)+" "+output.position_unit]])+
    table(["Setting","This result","Other result"],output.settings_changes.map(row=>[row.field,row.first??"—",row.second??"—"]))+
    control("Comparison CSV","variants-csv",{id:recipe.other}));
}

buttonActions["variants-csv"]=id=>download(ui.paths.result+"/variants",{other:id,download:true},"focalbench-recipe-comparison.csv");

const formActions={
  "capture-form":editCapture,
  "settings-form":async form=>{
    ui.railDraft=null;
    await updateProject({settings:fillRunDefaults(form)});toast("Reconstruction recipe saved.");
  },
  "deficits-form":findDeficits,
  "planes-form":showPlane,
  "pair-form":showPair,
  "variant-form":showRecipe,
};

document.addEventListener("click",async event=>{
  const button=event.target.closest("button[data-action]");
  if (!button||button.disabled) return;
  const action=buttonActions[button.dataset.action];
  if (!action) return;
  event.preventDefault();
  button.disabled=true;
  try { await action(button.dataset.id,button); }
  catch(error) { toast(error.message,true); }
  finally { if (button.isConnected) button.disabled=false; }
});

app.addEventListener("change",event=>{
  if (event.target.name==="rail_mode"&&event.target.form?.id==="settings-form") {
    const mode=event.target.value;
    const current=ui.railDraft??ui.stack.settings.scale;
    const draft={...current,rail_mode:mode};
    if (mode==="calibration"&&!draft.calibration) draft.calibration={points:[]};
    ui.railDraft=draft;
    // Show the block for the chosen mode without re-rendering, so values typed
    // in the other block are still there if the mode changes back.
    event.target.form.querySelectorAll("[data-rail-blocks]").forEach(block=>{
      block.hidden=block.dataset.railBlocks!==mode;
    });
  }
});
app.addEventListener("submit",async event=>{
  event.preventDefault();
  const action=formActions[event.target.id], button=event.submitter;
  if (!action) return;
  if (button) button.disabled=true;
  try { await action(new FormData(event.target)); }
  catch(error) {toast(error.message,true);}
  finally { if (button?.isConnected) button.disabled=false; }
});
window.addEventListener("hashchange",()=>followHash().catch(error=>toast(error.message,true)));
followHash().catch(async error=>{
  toast(error.message,true);
  try {await loadLibrary();}
  catch(cause) {app.innerHTML=`<p class="loading error">${html(cause.message)}</p>`;}
});
