import {attr,box,chart,check,control,field,html,link,metrics,n,percent,select,shortDate,stat,table,textArea,warning} from "./format.js";

export const LAYERS = [
  ["composite","All-in-focus"],["depth","Focus surface"],["confidence","Confidence"],
  ["coverage","Reliable coverage"],["plane","Winning plane"],["overlap","Frame overlap"],
];
export const layerName = value => LAYERS.find(([key])=>key===value)?.[1]??value;

export function libraryPage(list,demos) {
  return `<div class="library">
    <div class="library-head"><div><h1>Focus stack library</h1><p>Review capture planes, reconstruct an all-in-focus image and measure its focus surface.</p></div>
      <div class="row">${control("Import capture ZIP","import-zip")}</div></div>
    <div class="demo-grid">${demos.map(demo=>`<article class="demo-card">
      <div class="demo-preview"><img src="/static/${attr(demo.id)}.webp" alt="Synthetic ${attr(demo.name.toLowerCase())} target"></div>
      <div class="demo-content"><h2>${html(demo.name)}</h2><p>${html(demo.description)}<br><small>Synthetic · ${html(demo.sampling)}</small></p>
      ${control("Load "+demo.name.toLowerCase(),"demo",{id:demo.id,type:"accent"})}</div>
    </article>`).join("")}</div>
    <h2>Local stacks</h2><div class="library-list">${list.length?list.map(item=>`<div class="library-row">
      <div><strong>${html(item.name)}</strong> <small>${item.demo?"Synthetic":""}</small><div class="muted">${item.capture_count} captures · ${item.result_count} results</div></div>
      <small>${shortDate(item.changed_at)}</small>${control("Open","open-stack",{id:item.id})}</div>`).join(""):`<div class="empty-text">Start with a sample or create a stack for your own images.</div>`}</div>
  </div>`;
}

export function rail(ui,paths) {
  const project=ui.stack, items=project.captures.filter(frame=>ui.showArchived||!frame.archived);
  const count=project.captures.filter(frame=>frame.included&&!frame.archived).length;
  return `<aside class="stack-rail" aria-label="Stack captures and results">
    <div class="rail-head"><div><h2>Capture queue</h2><p class="rail-count">${count} included · ${items.length} shown</p></div>${control("Add","upload",{type:"small"})}</div>
    <div class="capture-list">${items.length?items.map(frame=>`<button type="button" class="capture-row ${ui.capture===frame.id&&ui.tab==="capture"?"active":""} ${frame.archived?"archived":""} ${!frame.included?"excluded":""}" data-action="select-capture" data-id="${attr(frame.id)}">
      <img src="${attr(paths.capture(frame.id)+"/preview")}" loading="lazy" alt=""><span>${html(frame.name)}<small class="mono">${n(frame.position,3)} ${html(project.settings.scale.rail_unit)}${frame.archived?" · archived":!frame.included?" · excluded":""}</small></span>
    </button>`).join(""):`<div class="empty-text">Add your focus images in capture order.</div>`}</div>
    ${project.captures.some(frame=>frame.archived)?control(ui.showArchived?"Hide archived frames":"Show archived frames","toggle-archived"):""}
    <div class="rail-buttons">${control("Preflight","preflight",{disabled:!items.length})}${control("Rail schedule","schedule")}${control("Contact sheet","contact-sheet",{disabled:!items.length})}${control("Rail log","rail-log",{disabled:!items.length})}</div>
    <div class="rail-head"><h2>Results</h2></div>
    <div class="result-list">${project.results.map(item=>`<button type="button" class="result-row ${ui.result?.id===item.id&&ui.tab==="result"?"active":""}" data-action="open-result" data-id="${attr(item.id)}">${html(item.title)}
      <small>${item.capture_count} planes · ${percent(item.reliable_fraction)} reliable</small></button>`).join("")||`<p class="rail-count">Reconstructed results will appear here.</p>`}</div>
  </aside>`;
}

export function capturePanel(ui) {
  const frame=ui.stack.captures.find(item=>item.id===ui.capture), path=ui.paths.stack;
  const info = frame?.information;
  return `<aside class="right-pane" aria-label="Capture controls">
    <div class="panel-tabs">${control("Capture","capture-panel",{id:"capture",selected:ui.capturePanel==="capture"})}${control("Reconstruction","capture-panel",{id:"settings",selected:ui.capturePanel==="settings"})}</div>
    ${ui.capturePanel==="settings"?settingsForm(ui):frame?`
      ${box("Capture metadata",`
        <p>${html(frame.name)}</p>
        ${stat("Orientation corrected",frame.width+" × "+frame.height)}
        ${stat("Sample format",info.format+" · "+info.bit_depth+" bit")}
        ${stat("Profile",info.profile)}
        ${info.exposure_seconds?stat("Exposure",n(info.exposure_seconds,5)+" s"):""}
        ${info.iso?stat("ISO",n(info.iso,0)):""}
        ${info.lens_model?stat("Lens",info.lens_model):""}
        ${stat("Capture slot",String(frame.slot))}
        ${frame.archived?warning("Archived after a retake."):""}
      `)}
      ${frame.archived?"":`<form id="capture-form" class="box stack"><h3>Focus position</h3>${field("Rail position ("+ui.stack.settings.scale.rail_unit+")","position",frame.position,{type:"number",step:"any",required:true})}
          ${check("Include in reconstruction","enabled",frame.included)}${textArea("Capture note","note",frame.note)}<button type="submit">Save capture</button>
      </form>`}
      ${box("Source inspection",`<div class="row">${control("Exposure histogram","exposure")}${frame.archived?"":control("Retake","retake")}${link("Original image",`${path}/captures/${frame.id}/source`)}</div>`)}
    `:`<p class="empty-text">Select a capture to inspect its metadata and focus position.</p>`}
    ${box("Stack operations",`<div class="row">${control("Name and notes","stack-meta")}${control("Fork stack","fork-stack")}${ui.stack.captures.length?link("Capture ZIP",path+"/portable"):""}</div>`)}
  </aside>`;
}

function settingsForm(ui) {
  const config=ui.stack.settings;
  const references=ui.stack.captures.filter(item=>item.included&&!item.archived).map(item=>[item.id,item.name+" ("+n(item.position)+")"]);
  const light=config.lighting??{};
  return `<form id="settings-form" class="box stack">
    <h3>Reconstruction recipe</h3>
    ${select("Registration","registration",[["breathing","Translate and scale"],["translation","Translation only"],["none","Already aligned"]],config.registration)}
    ${select("Reference capture","reference_id",[["","Middle enabled plane"],...references],config.reference_id??"")}
    ${select("Focus response","score",[["fine","Fine detail"],["balanced","Balanced"],["broad","Broad features"]],config.score)}
    ${select("Fusion","blend",[["winner","Strongest capture"],["feather","Feathered weights"],["pyramid","Multiscale blend"]],config.blend)}
    ${select("Viewport","viewport",[["reference","Full reference image"],["shared","Shared coverage crop"]],config.viewport)}
    <div class="field-row">${field("Confidence floor","confidence_floor",config.confidence_floor,{type:"number",min:0,max:.95,step:.01})}${field("Guard border (px)","edge_guard",config.edge_guard,{type:"number",min:0,max:12,step:1})}</div>
    ${field("Fusion strength","strength",config.strength,{type:"number",min:.1,max:1,step:.05})}
    <div class="field-row">${select("Rail unit","rail_unit",[["µm","µm"],["mm","mm"],["step","Stage steps"]],config.scale.rail_unit)}${field("Rail origin","rail_offset",config.scale.rail_offset,{type:"number",step:"any"})}</div>
    ${field("Reference pixel size (µm / px)","pixel_um",config.scale.pixel_um??"",{type:"number",step:"any",placeholder:"Uncalibrated"})}
    <button type="submit">Save recipe</button>
    <div class="subhead"><h3>Light matching</h3>${light.region?"Patch saved":"No patch selected"}</div>
    <p class="muted">Select one evenly lit area of the same material. Each capture is scaled to the reference there; a capture that cannot be compared is left out of the result and named.</p>
    ${["lighting_region_left","lighting_region_top","lighting_region_right","lighting_region_bottom"].map((name,index)=>{
      const key=["left","top","right","bottom"][index];
      return field("Patch "+key,name,(light.region??{})[key]??"",{type:"number",min:0,max:1,step:.001});
    }).join("")}
    <div class="row">${control("Use current selection","lighting-from-selection",{disabled:!["capture","result"].includes(ui.tab)||ui.selection?.kind!=="area"})}${light.region?control("Clear patch","lighting-clear"):""}</div>
    <p class="muted">Coordinates are in the reference frame. Draw on the reference capture, or pick an area in a result and use it here.</p>
  </form>`;
}

const pathFor = (ui,suffix) => ui.paths.result+suffix;

export function resultPanel(ui) {
  const cfg=ui.result.summary, selection=ui.selection, note=ui.resultPanel;
  const area=selection.kind==="area", section=selection.kind==="section";
  let content;
  if (note==="planes") content = planePanel(ui);
  if (note==="compare") content = comparePanel(ui);
  if (note==="notebook") content = notebookPanel(ui);
  if (note==="measure") {
    content = `${box("Archived reconstruction",`
      ${stat("Reference",cfg.reference_name)}${stat("Viewport",cfg.settings.viewport==="shared"?"Shared coverage":"Reference image")}
      ${stat("XY scale",cfg.scale.pixel_um===null?"Uncalibrated":n(cfg.scale.pixel_um,4)+" µm / px")}
      ${stat("Rail origin",n(cfg.scale.rail_offset)+" "+cfg.scale.rail_unit)}
      ${stat("Reliable focus",percent(cfg.reliable_fraction))}
      ${stat("Shared frame overlap",percent(cfg.coverage.shared_fraction))}
      ${cfg.warnings.length?`<details><summary>${cfg.warnings.length} observation${cfg.warnings.length===1?"":"s"}</summary>${cfg.warnings.map(item=>warning(item.detail)).join("")}</details>`:""}
    `)}
    ${box("Light matching",lightingPanel(ui))}
    ${box("Selection actions",`
       <div class="row">${control("Sharp interval","sharp-width",{disabled:!area})}${control("Export patch","patch",{disabled:!area})}
       ${control("Use as rail datum","datum",{disabled:!area})}${control("Calibrate pixel scale","calibrate",{disabled:!section})}
       ${control("Light patch","lighting-from-selection",{disabled:!area,title:"Use this area as the light-matching patch for the next reconstruction"})}
       ${control("Section CSV","section-csv",{disabled:!section})}</div>
    `)}
    ${box("Coverage details",`<form id="deficits-form" class="stack">
      ${select("Show","kind",[["both","All deficient regions"],["weak","Weak focus only"],["missing","Missing image support"]],ui.deficitKind)}
      ${field("Minimum region pixels","minimum",ui.deficitMinimum,{type:"number",min:1,step:1})}
      ${check("Limit to selection","scoped",false)}
      <button type="submit">Find gaps</button></form>`)}
    ${box("Evidence",`<div class="row">${link("Evidence ZIP",pathFor(ui,"/evidence"))}${link("Depth PNG (16-bit)",pathFor(ui,"/images/depth16?download=1"))}
    ${link("Inspection proof",pathFor(ui,"/proof.png?download=1"))}${control("Verify sources","integrity")}${control("Fork result","fork-result")}</div>`)}`;
  }
  return `<aside class="right-pane" aria-label="Result tools"><div class="panel-tabs">
    ${[["measure","Measures"],["planes","Planes"],["compare","Compare"],["notebook","Notebook"]].map(([key,name])=>control(name,"result-panel",{id:key,selected:note===key})).join("")}
  </div>${content??""}</aside>`;
}

function lightingPanel(ui) {
  const light=ui.result.summary.lighting;
  if (!light) return `<p class="muted">No material patch was part of this recipe, so every capture kept its recorded brightness.</p>`;
  const labels={reference:"Reference",direct:"Matched",bridged:"Bridged",unmatched:"Left out"};
  const rows=light.captures.map(item=>[
    item.name,labels[item.status]??item.status,
    item.status==="unmatched"?"—":n(item.gain,4)+"×",
    item.hops?String(item.hops):"—",
    item.reason??"—",
  ]);
  const excluded=light.excluded_ids?.length
    ? light.captures.filter(item=>light.excluded_ids.includes(item.id)).map(item=>item.name)
    : light.captures.filter(item=>item.status==="unmatched").map(item=>item.name);
  return `${metrics([["Patch samples",n(light.samples,0)],["Corrected",n(light.corrected,0)],["Left out",n(light.unmatched,0)]])}
    ${excluded.length?warning(`Left out of this reconstruction for lack of a trustworthy light comparison: ${excluded.join(", ")}. Their original files remain in the stack.`):""}
    <p class="muted">A gain multiplies each aligned capture before focus evidence. Bridged captures were matched through other frames in the patch.</p>
    ${table(["Capture","Light","Gain","Hops","Note"],rows)}`;
}
function planePanel(ui) {
  const frames=ui.result.summary.captures;
  const unit=ui.result.summary.scale.rail_unit;
  return `${box("Source contribution",`<form id="planes-form" class="stack">
    ${select("Capture","capture",frames.map(item=>[item.id,item.name+" · "+n(item.display_position)+" "+unit]),ui.plane??frames[0]?.id)}
    ${select("Display","view",[["highlight","Highlight reliable winners"],["registered","Aligned capture"],["mask","Winner mask"]],ui.planeView)}
    ${field("Minimum island pixels","minimum",ui.planeMinimum,{type:"number",min:1,step:1})}<button type="submit">Inspect plane</button>
  </form>`)}
  ${box("Capture evidence",`${table(["Rail "+unit,"Shift","Zoom","Wins"],frames.map(item=>[n(item.display_position,2),n(item.motion.dx,1)+", "+n(item.motion.dy,1),n(item.motion.scale,4),percent(item.winner_fraction)]))}
    ${link("Capture evidence CSV",pathFor(ui,"/captures.csv"))}`)}`;
}

function comparePanel(ui) {
  const frames=ui.result.summary.captures;
  const captures=frames.map(item=>[item.id,item.name]);
  const archived=ui.stack.results.filter(item=>item.id!==ui.result.id).map(item=>[item.id,item.title]);
  return `${box("Capture pair",`<form id="pair-form" class="stack">
    ${select("First capture","first",captures,ui.pair.first??captures[0]?.[0])}
    ${select("Second capture","second",captures,ui.pair.second??captures.at(-1)?.[0])}
    ${select("Coordinates","alignment",[["registered","Aligned"],["raw","Before alignment"]],ui.pair.alignment)}
    ${select("Display","mode",[["overlay","Color overlay"],["split","Split view"],["difference","Luminance difference"],["side","Side by side"]],ui.pair.mode)}
    ${field("Divider / gain","amount",ui.pair.amount,{type:"range",min:0,max:1,step:.01})}<button type="submit">Compare captures</button>
    </form>`)}
    ${box("Reconstruction recipes",archived.length?`<form id="variant-form" class="stack">
    ${select("Other result","other",archived,ui.recipe.other??archived[0][0])}
    ${select("Display","view",[["split","Composite split"],["difference","Depth difference"],["support","Joint reliability"]],ui.recipe.view)}
    ${field("Divider / gain","amount",ui.recipe.amount,{type:"range",min:0,max:1,step:.01})}
    <button type="submit">Compare results</button></form>`:`<p>Reconstruct again with the same reference to compare recipes.</p>`)}`;
}

function notebookPanel(ui) {
  const markLabel = ({pin:"Point",line:"Section",area:"Area"});
  return `${box("Image annotations",`
    <div class="row">${control("Annotate selection","mark",{disabled:!["pin","section","area","polygon"].includes(ui.selection.kind)})}
      ${control("Draw polygon","tool",{id:"polygon",selected:ui.tool==="polygon"})}
      ${ui.tool==="polygon"?control("Finish polygon","finish-polygon",{disabled:ui.pending.length<3}):""}</div>
    ${ui.marks.map(mark=>`<div class="box"><div class="subhead"><h3>${html(mark.label)}</h3>${control("Delete","delete-mark",{id:mark.id,type:"quiet danger"})}</div>
      <p>${html(markLabel[mark.kind])} · ${html(mark.color)}${mark.note?" · "+html(mark.note):""}</p></div>`).join("")}
    ${ui.marks.length?link("Annotations CSV",pathFor(ui,"/marks.csv")):""}
  `)}
  ${box("Saved inspections",`
    <div class="row">${control("Save reading","save-card",{disabled:!!ui.override||ui.selection.kind==="polygon"})}</div>
    ${ui.cards.map(card=>`<div class="box"><div class="subhead"><h3>${html(card.title)}</h3>${control("Delete","delete-card",{id:card.id,type:"quiet danger"})}</div>
      <p>${html(layerName(card.content.layer))} · ${html(card.content.selection.kind)}</p>
      <div class="row">${control("Reopen","open-card",{id:card.id})}${link("Proof PNG",pathFor(ui,`/cards/${card.id}/proof.png?download=1`),{small:true})}</div></div>`).join("")}
  `)}`;
}

export function resultOverview(summary) {
  const depth=summary.depth_statistics, rail=summary.scale.rail_unit;
  return metrics([
    ["Visible area",percent(summary.visible_fraction)],["Reliable focus",percent(summary.reliable_fraction)],
    ["Median depth",n(depth.median)+" "+rail],["Depth P05–P95",n(depth.p05)+" – "+n(depth.p95)+" "+rail],
  ]);
}

export function readoutPanel(title,body) {
  return `<section class="readout-panel"><h2>${html(title)}</h2>${body}</section>`;
}

export function measurementReadout(selection,data) {
  if (selection.kind==="pin") return [
    data.reliable?"Point reading":"Point · insufficient focus evidence",
    metrics([["Focus position",n(data.position)+" "+data.unit],["Confidence",percent(data.confidence)],["Contributors",data.support],["Original x / y",n(data.source_x,1)+" / "+n(data.source_y,1)]])+chart("Focus response across capture positions"),
    {unit:data.unit,ymin:0,series:[{points:data.response.map(row=>[row.position,row.response])}]},
  ];
  if (selection.kind==="section") return [
    "Depth section",
    metrics([["Length",n(data.length_um??data.length_px)+" "+(data.length_um===null?"px":"µm")],["Reliable samples",percent(data.reliable_fraction)],["Depth min / max",n(data.minimum)+" / "+n(data.maximum)+" "+data.unit]])+chart("Depth along the selected section"),
    {unit:data.length_um===null?"px":"µm",series:[{color:"#8dd5bd",points:data.samples.map(row=>[row.distance_um??row.distance,row.position])}]},
  ];
  return [
    "Area measurement",
    metrics([["Selected pixels",n(data.pixels,0)],["Reliable focus",percent(data.reliable_fraction)],["Median depth",n(data.positions.median)+" "+data.unit],["P05–P95",n(data.positions.p05)+" – "+n(data.positions.p95)]])+chart("Depth histogram of reliable focus in the area"),
    {unit:data.unit,ymin:0,series:[{bars:true,points:data.histogram.counts.map((count,i)=>[(data.histogram.edges[i]+data.histogram.edges[i+1])/2,count])}]},
  ];
}
