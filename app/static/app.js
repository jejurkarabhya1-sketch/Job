const $ = id => document.getElementById(id);
const sources = ["linkedin","indeed","glassdoor","zip_recruiter","google","bayt","naukri","jobicy","remoteok","arbeitnow_eu","arbeitnow_uk"];
const sourceCoverage = {
  linkedin:"Worldwide · no location filter",
  indeed:"No city filter · defaults to US",
  glassdoor:"No location filter · availability varies",
  zip_recruiter:"Primarily North America",
  google:"No location filter · availability varies",
  bayt:"Middle East and North Africa",
  naukri:"Primarily India",
  jobicy:"Remote · worldwide regions · recent 7-day feed",remoteok:"Remote · worldwide",
  arbeitnow_eu:"Europe",arbeitnow_uk:"United Kingdom"
};
let allJobs = [];
let searchRunning = false;
let currentLocation = "Worldwide";

let locationTimer = null;
let locationRequest = 0;

function toast(message){
  const node=$("toast"); node.textContent=message; node.classList.add("show");
  setTimeout(()=>node.classList.remove("show"),3500);
}
async function api(url, options={}){
  const response=await fetch(url,{headers:{"Content-Type":"application/json"},...options});
  const data=await response.json();
  if(!response.ok) throw new Error(data.detail||"Request failed");
  return data;
}
function labelFor(source){
  return ({
    zip_recruiter:"ZipRecruiter",google:"Google Jobs",
    remoteok:"Remote OK",arbeitnow_eu:"Arbeitnow Europe",arbeitnow_uk:"Arbeitnow UK"
  })[source]
    || source.charAt(0).toUpperCase()+source.slice(1);
}
function initSources(){
  $("source-options").innerHTML=sources.map(source=>`
    <label class="portal-option">
      <input type="checkbox" value="${source}" ${["linkedin","indeed","glassdoor","google","jobicy","remoteok"].includes(source)?"checked":""}>
      <span class="switch"></span><span class="portal-info"><span class="portal-name">${labelFor(source)}</span>
      ${sourceCoverage[source]?`<small class="portal-coverage">${sourceCoverage[source]}</small>`:""}</span>
    </label>`).join("");
  $("source-options").addEventListener("change",updateSelectedCount);
  updateSelectedCount();
}
function updateSelectedCount(){
  const count=selectedSources().length;
  $("selected-count").textContent=`${count} portal${count===1?"":"s"} selected`;
}
function selectedSources(){
  return [...document.querySelectorAll("#source-options input:checked")].map(input=>input.value);
}
$("select-all").onclick=()=>{
  document.querySelectorAll("#source-options input").forEach(input=>input.checked=true);
  updateSelectedCount();
};
$("clear-all").onclick=()=>{
  document.querySelectorAll("#source-options input").forEach(input=>input.checked=false);
  updateSelectedCount();
};

function esc(value){
  return String(value??"").replace(/[&<>"']/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
}
function safeUrl(value){
  try{const url=new URL(value);return ["http:","https:"].includes(url.protocol)?url.href:"#";}
  catch{return "#";}
}
function renderJobs(items){
  allJobs=items;
  updatePortalFilter();
  applyResultFilters();
}
function updatePortalFilter(){
  const select=$("portal-filter");
  const previous=select.value;
  const available=[...new Set(allJobs.map(job=>job.source||"").filter(Boolean))].sort();
  select.innerHTML='<option value="">All portals</option>'+available.map(source=>`
    <option value="${esc(source)}">${esc(labelFor(source))}</option>`).join("");
  select.value=available.includes(previous)?previous:"";
}
function applyResultFilters(){
  const query=$("filter").value.trim().toLowerCase();
  const portal=$("portal-filter").value;
  const filtered=allJobs.filter(job=>{
    const matchesText=[job.title,job.company,job.location,job.source].join(" ").toLowerCase().includes(query);
    const matchesPortal=!portal||(job.source||"").toLowerCase()===portal.toLowerCase();
    return matchesText&&matchesPortal;
  });
  $("job-count").textContent=filtered.length;
  $("job-rows").innerHTML=filtered.length?filtered.map((job,index)=>`
    <tr>
      <td class="row-number" data-label="#">${index+1}</td>
      <td class="job-title-cell"><strong>${esc(job.title)}</strong><span>${esc(job.company||"Company not listed")}</span></td>
      <td data-label="Location">${esc(job.location||"—")}</td>
      <td data-label="Portal"><span class="source-tag">${esc(labelFor(job.source||"—"))}</span></td>
      <td data-label="Posted">${esc(job.date_posted||"—")}</td>
      <td data-label="Experience">${esc(job.experience_range||"Not specified")}</td>
      <td class="job-link-cell"><a href="${safeUrl(job.job_url)}" target="_blank" rel="noopener">View job ↗</a></td>
    </tr>`).join(""):`<tr><td colspan="7" class="empty">No matching jobs found. Try another keyword, location or portal.</td></tr>`;
}
async function loadJobs(location=currentLocation, selected=selectedSources()){
  const params=new URLSearchParams({limit:"500",days:"30"});
  if(location)params.set("location",location);
  if(selected.length)params.set("source",selected.join(","));
  const data=await api("/api/jobs?"+params.toString());
  renderJobs(data.items);
}
async function waitForSearch(startedAt, selected){
  for(let attempt=0;attempt<180;attempt++){
    await new Promise(resolve=>setTimeout(resolve,5000));
    try{
      const portalStatus=await api("/api/search/status");
      if(portalStatus.sources){
        const labels=Object.entries(portalStatus.sources).map(([source,info])=>{
          const name=labelFor(source);
          if(info.status==="connecting") return `Connecting: ${name}`;
          if(info.status==="failed") return `Failed: ${name}${info.error?" · "+info.error:""}`;
          return `Connected: ${name} · ${info.count||0} jobs`;
        });
        $("search-message").innerHTML=`
          <strong>${esc(portalStatus.status==="running"?"Searching portals":"Search finished")}</strong>
          <span class="portal-status-list">${labels.map(label=>`<span>${esc(label)}</span>`).join("")}</span>`;
        $("search-message").className="search-status searching";
      }
      const history=await api("/api/searches");
      const latest=history.find(item=>new Date(item.created_at+"Z").getTime()>=startedAt-3000);
      if(latest){
        $("search-message").textContent=latest.status==="completed"
          ? `Search complete · ${latest.result_count} jobs found`
          : latest.status==="failed" ? "Search failed. Check the backend logs and try again."
          : "Searching selected portals…";
        if(latest.status==="completed"||latest.status==="failed"){
          await loadJobs(currentLocation, selected);
          if(latest.status==="failed") toast("Search failed: "+(latest.error||"Unknown error"));
          else toast(`Search completed: ${latest.result_count} jobs found.`);
          return;
        }
      }else{
        $("search-message").textContent="Connecting to job portals and collecting listings…";
      }
    }catch(error){$("search-message").textContent="Waiting for search results…";}
  }
  $("search-message").textContent="Search is taking longer than expected. Use Refresh to check for results.";
}
$("search-form").addEventListener("submit",async event=>{
  event.preventDefault();
  $("location-list").hidden=true;
  if(searchRunning)return;
  const chosen=selectedSources();
  if(!chosen.length){toast("Select at least one job portal.");return;}
  const button=$("search-button");
  searchRunning=true;button.disabled=true;button.innerHTML="Searching…";
  $("search-message").textContent="Starting search across selected portals…";
  $("job-rows").innerHTML='<tr><td colspan="7" class="empty">Searching selected portals. Job results will appear here when the search finishes…</td></tr>';
  const startedAt=Date.now();
  try{
    currentLocation=$("location").value.trim()||"Worldwide";
    await api("/api/search",{method:"POST",body:JSON.stringify({
      search_term:$("term").value.trim(),
      location:currentLocation,
      sources:chosen,
      results_wanted:Number($("count").value),
      hours_old:Number($("age").value),
      experience:$("experience").value
    })});
    await waitForSearch(startedAt, chosen);
  }catch(error){
    $("search-message").textContent="Could not start the search.";
    toast(error.message);
  }finally{
    searchRunning=false;button.disabled=false;button.innerHTML='<span class="search-symbol">⌕</span> Search jobs';
  }
});
$("refresh").onclick=async()=>{try{await loadJobs();toast("Job results refreshed.");}catch(error){toast(error.message);}};
$("filter").addEventListener("input",applyResultFilters);
$("portal-filter").addEventListener("change",applyResultFilters);
initSources();
async function updateLocationSuggestions(){
  const input=$("location");
  const query=input.value.trim().toLowerCase();
  const requestId=++locationRequest;
  if(query.length<2||["worldwide","global","anywhere","remote worldwide","remote anywhere"].includes(query)){
    $("location-list").hidden=true;
    return;
  }
  try{
    const items=await api("/api/locations?q="+encodeURIComponent(input.value.trim()));
    if(requestId!==locationRequest)return;
    const list=$("location-list");
    list.innerHTML=items.length?items.map((item,index)=>`
      <button type="button" class="location-option" role="option" data-index="${index}">
        ${esc(item.label)}
        <small>${esc(item.display_name)}</small>
      </button>`).join(""):"";
    list.hidden=!items.length;
    [...list.querySelectorAll(".location-option")].forEach((button,index)=>{
      const choose=event=>{
        event.preventDefault();
        input.value=items[index].label;
        list.hidden=true;
      };
      button.onmousedown=choose;
      button.onclick=choose;
    });
  }catch(error){
    if(requestId===locationRequest)$("location-list").hidden=true;
  }
}
$("location").addEventListener("input",()=>{
  clearTimeout(locationTimer);
  locationTimer=setTimeout(updateLocationSuggestions,300);
});
$("location").addEventListener("blur",()=>{
  setTimeout(()=>{$("location-list").hidden=true;},150);
});
$("location").addEventListener("keydown",event=>{
  if(event.key==="Escape")$("location-list").hidden=true;
  if(event.key==="ArrowDown"){
    event.preventDefault();
    const first=$("location-list").querySelector(".location-option");
    if(first){$("location-list").hidden=false;first.focus();}
  }
});
$("location-list").addEventListener("keydown",event=>{
  const options=[...$("location-list").querySelectorAll(".location-option")];
  const index=options.indexOf(document.activeElement);
  if(event.key==="ArrowDown"&&index>=0&&index<options.length-1){event.preventDefault();options[index+1].focus();}
  if(event.key==="ArrowUp"){
    event.preventDefault();
    if(index>0)options[index-1].focus();
    else $("location").focus();
  }
  if(event.key==="Enter"&&index>=0)options[index].click();
});
updateLocationSuggestions();
loadJobs(currentLocation).catch(error=>console.error("Could not load saved jobs:",error));
