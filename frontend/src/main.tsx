import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { AudioLines, Check, ChevronDown, CircleHelp, FileAudio, FileVideo, Languages, Mic, Play, Plus, ShieldCheck, Trash2, Upload, WandSparkles } from 'lucide-react';
import './styles.css';

type Project = { id: string; name: string; source_lang: string; target_langs: string[]; duration_s?: number; status: string };
type Clip = { id: string; idx: number; start_s: number; end_s: number; src_text: string; translation?: string; status?: string; reasons?: string[]; attempts?: unknown[]; human_edited?: number };
type Term = {id:string;term:string;type:string;treatment:string;locked:boolean;targets:Record<string,string>};
const API = '/api';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, init);
  if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
  return response.json();
}

function App() {
  const [config, setConfig] = useState<any>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [clips, setClips] = useState<Clip[]>([]);
  const [terms, setTerms] = useState<Term[]>([]);
  const [estimate, setEstimate] = useState<any>(null);
  const [languages, setLanguages] = useState<string[]>(['hi-IN']);
  const [source, setSource] = useState('auto');
  const [name, setName] = useState('My first dub');
  const [video, setVideo] = useState<File | null>(null);
  const [recording, setRecording] = useState(false);
  const [videoRecording, setVideoRecording] = useState(false);
  const [recordSeconds, setRecordSeconds] = useState(0);
  const [contentType, setContentType] = useState('explainer');
  const [translationStyle, setTranslationStyle] = useState('neutral');
  const [consentPhrase, setConsentPhrase] = useState('');
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [compare, setCompare] = useState(false);
  const [activeLang, setActiveLang] = useState('hi-IN');
  const [jobMessage, setJobMessage] = useState('');
  const [mediaUrl, setMediaUrl] = useState('');
  const [consentBlob, setConsentBlob] = useState<Blob | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const videoRecorder = useRef<MediaRecorder | null>(null);
  const videoStream = useRef<MediaStream | null>(null);
  const recordTimer = useRef<number | null>(null);
  const chunks = useRef<Blob[]>([]);
  const videoChunks = useRef<Blob[]>([]);

  useEffect(() => {
    request<any>('/config').then(setConfig).catch((e) => setError(e.message));
    refreshProjects();
  }, []);

  async function refreshProjects() { setProjects(await request<Project[]>('/projects')); }
  async function openProject(item: Project) {
    setProject(item); setActiveLang(item.target_langs[0] || 'hi-IN');
    setClips(await request<Clip[]>(`/projects/${item.id}/clips?lang=${item.target_langs[0] || 'hi-IN'}`));
    const sheet = await request<{terms:Term[]}>(`/projects/${item.id}/termsheet`); setTerms(sheet.terms);
    if(item.duration_s) setEstimate(await request(`/projects/${item.id}/estimate`));
    setNotice('Project opened.');
  }
  async function createDub() {
    if (!video || !languages.length) return;
    setBusy(true); setError('');
    try {
      const created = await request<Project>('/projects', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({name, source_lang: source, target_langs: languages}) });
      const form = new FormData(); form.append('video', video);
      const uploaded = await request<{duration_s:number}>(`/projects/${created.id}/video`, {method:'PUT', body:form});
      const complete = {...created, duration_s:uploaded.duration_s, status:'uploaded'};
      setProject(complete); setNotice(`Uploaded ${uploaded.duration_s.toFixed(1)} seconds. Next, record the consent phrase and add your clean voice sample.`);
      setEstimate(await request(`/projects/${complete.id}/estimate`));
      refreshProjects();
    } catch (e:any) { setError(e.message); } finally { setBusy(false); }
  }
  async function getConsentPhrase() {
    const result = await request<{phrase:string;notice:string}>('/voices/consent-phrase');
    setConsentPhrase(result.phrase); setNotice(result.notice);
  }
  async function startConsentRecording() {
    setError('');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({audio:true});
      recorder.current = new MediaRecorder(stream); chunks.current = [];
      recorder.current.ondataavailable = (event) => chunks.current.push(event.data);
      recorder.current.onstop = () => { setConsentBlob(new Blob(chunks.current, {type: recorder.current?.mimeType || 'audio/webm'})); stream.getTracks().forEach((track) => track.stop()); setRecording(false); };
      recorder.current.start(); setRecording(true);
    } catch { setError('Microphone access is needed to record the live consent phrase.'); }
  }
  function stopConsentRecording() { recorder.current?.stop(); }
  async function startVideoRecording() {
    setError('');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({video:true,audio:true});
      videoStream.current = stream; videoChunks.current = [];
      videoRecorder.current = new MediaRecorder(stream);
      videoRecorder.current.ondataavailable = (event) => videoChunks.current.push(event.data);
      videoRecorder.current.onstop = () => {
        setVideo(new File([new Blob(videoChunks.current,{type:videoRecorder.current?.mimeType||'video/webm'})], 'recorded-video.webm', {type:'video/webm'}));
        videoStream.current?.getTracks().forEach((track)=>track.stop()); setVideoRecording(false);
        if(recordTimer.current) window.clearInterval(recordTimer.current);
      };
      videoRecorder.current.start(); setVideoRecording(true); setRecordSeconds(0);
      recordTimer.current = window.setInterval(()=>setRecordSeconds((seconds)=>{ if(seconds>=119){videoRecorder.current?.stop();return 120;}return seconds+1;}),1000);
    } catch { setError('Camera and microphone access are needed to record a video.'); }
  }
  async function saveVoice(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!consentBlob) { setError('Record the consent phrase live first.'); return; }
    const form = new FormData(event.currentTarget); form.append('consent_audio', consentBlob, 'consent.webm');
    try {
      setBusy(true); const result = await request<{id:string}>('/voices', {method:'POST', body:form});
      const job = await request<{job_id:string}>(`/projects/${project!.id}/start`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({voice_id:result.id,content_type:contentType,translation_style:translationStyle})});
      setProject({...project!,status:'processing'});
      setNotice('Voice consent confirmed. Processing started.'); setJobMessage('Queued');
      const events = new EventSource(`${API}/projects/${project!.id}/events`);
      events.onmessage = (message) => { const update = JSON.parse(message.data); setJobMessage(update.message); if (['complete','failed'].includes(update.stage)) { events.close(); setProject({...project!,status:update.stage}); refreshProjects(); request<Clip[]>(`/projects/${project!.id}/clips?lang=${activeLang}`).then(setClips); request<{terms:Term[]}>(`/projects/${project!.id}/termsheet`).then((sheet)=>setTerms(sheet.terms)); } };
      void job;
    } catch (e:any) { setError(e.message); } finally { setBusy(false); }
  }
  async function changeLanguage(lang: string) {
    setActiveLang(lang); if (project) setClips(await request<Clip[]>(`/projects/${project.id}/clips?lang=${lang}`));
  }
  async function saveTranslation(clip: Clip, translation: string) {
    if (!clip.id) return;
    const result = await request<{status:string;reasons:string[]}>(`/clips/${clip.id}/dubs/${activeLang}`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({translation})});
    setClips((current) => current.map((row) => row.id === clip.id ? {...row, translation, status:result.status, reasons:result.reasons, human_edited:1} : row));
  }
  async function exportFiles() {
    if (!project) return;
    try { const result = await request<any>(`/projects/${project.id}/export`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lang:activeLang,formats:['mp4','srt','audio','report']})}); setNotice(`AI dubbed export ready: ${Object.values(result.files).join(', ')}`); } catch(e:any) { setError(e.message); }
  }
  async function saveTerms() {
    if(!project)return;
    await request(`/projects/${project.id}/termsheet`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({terms:terms.map(({id,...term})=>term),idioms:[]})});
    setNotice('Term sheet saved. Locked terms are checked in clip translations.');
  }
  async function deleteProject() { if (!project) return; await request(`/projects/${project.id}`, {method:'DELETE'}); setProject(null); refreshProjects(); }
  async function deleteAllData() { if (!window.confirm('Delete all projects, uploads, voice references and generated dubs from this workspace? This cannot be undone.')) return; await request('/data', {method:'DELETE'}); setProject(null); setProjects([]); setNotice('All workspace data was deleted.'); }

  const targetOptions = Object.entries(config?.languages || {}) as [string,string][];
  const counts = clips.reduce((acc, clip) => { acc[clip.status || 'pending'] = (acc[clip.status || 'pending'] || 0) + 1; return acc; }, {} as Record<string,number>);

  return <div className="app-shell">
    <aside className="sidebar">
      <button className="brand" onClick={() => {setProject(null);setNotice('')}}><span className="brand-mark">a.</span><span>Anek <ChevronDown size={15}/></span></button>
      <div className="nav-label">STUDIO</div><button className={!project?'nav-item active':'nav-item'} onClick={()=>setProject(null)}><FileVideo size={17}/> Projects</button><button className="nav-item" onClick={()=>{setProject(null);setNotice('Choose a video to start a new dub.')}}><Plus size={17}/> New dub</button>
      <div className="nav-label">TRUST</div><div className="nav-item static"><ShieldCheck size={17}/> Verify every clip</div>
      <div className="sidebar-bottom"><div className="credit-card"><span className="credit-icon"><AudioLines size={18}/></span><div><strong>{config?.daily_credit_limit ?? '—'} credits</strong><small>Daily safety limit</small></div></div><div className="profile"><span className="avatar">A</span><span><strong>Creator workspace</strong><small>Local session</small></span></div></div>
    </aside>
    <main className="main-content">
      <header className="topbar"><div><div className="eyebrow">ANEK STUDIO <span className="beta">BETA</span></div><h1>{project?.name || 'Your projects'}</h1></div><button className="icon-button" title="Help"><CircleHelp size={18}/></button></header>
      {error && <div className="alert error" role="alert">{error}<button onClick={()=>setError('')}>Dismiss</button></div>}
      {notice && <div className="alert notice" role="status">{notice}</div>}
      {!project ? <section className="workspace">
        <div className="intro-row"><div><h2>Speak once. India understands.</h2><p>Check each translated line, number, and name before you publish.</p></div><span className="info-pill"><ShieldCheck size={15}/> AI dubbed · 2 min max</span></div>
        <div className="card create-card"><div className="card-heading"><span className="icon-tile"><Upload size={18}/></span><div><h3>Create a dub</h3><p>Single-speaker, speech-led videos work best.</p></div></div>
          <label className="field-label">Project name<input value={name} onChange={(e)=>setName(e.target.value)} /></label>
          <label className="field-label">Upload video <span className="soft">MP4, MOV or WebM · maximum 2 minutes</span><input type="file" accept="video/mp4,video/quicktime,video/webm" onChange={(e)=>setVideo(e.target.files?.[0] || null)} /></label>
          <div className="button-row">{videoRecording?<button className="danger-button" onClick={()=>videoRecorder.current?.stop()}>Stop camera · {Math.floor(recordSeconds/60)}:{String(recordSeconds%60).padStart(2,'0')}</button>:<button className="secondary-button" onClick={startVideoRecording}><Mic size={15}/> Record a video</button>}{video&&<span className="ok-label"><Check size={15}/> Video ready</span>}</div>
          {video && <div className="file-pill"><FileVideo size={16}/>{video.name}<span>{(video.size/1024/1024).toFixed(1)} MB</span></div>}
          <div className="form-grid"><label className="field-label">Source language<select value={source} onChange={(e)=>setSource(e.target.value)}><option value="auto">Auto-detect</option>{targetOptions.map(([code,label])=><option key={code} value={code}>{label}</option>)}</select></label><label className="field-label">Speakers<select disabled value="1"><option>1 speaker</option></select><small>Multi-speaker is not supported yet.</small></label></div>
          <div className="field-label">Target languages <span className="soft">{languages.length} selected</span></div><div className="language-grid">{targetOptions.map(([code,label])=><label className={languages.includes(code)?'language-option selected':'language-option'} key={code}><input type="checkbox" checked={languages.includes(code)} onChange={(e)=>setLanguages(e.target.checked?[...languages,code]:languages.filter((item)=>item!==code))}/><span>{label}</span><small>{code}</small></label>)}</div>
          <details className="advanced"><summary>Advanced options</summary><label className="field-label">Content type<select value={contentType} onChange={(e)=>setContentType(e.target.value)}><option value="lecture">Lecture / Education</option><option value="explainer">Explainer / News</option><option value="podcast">Podcast / Talk</option><option value="sports">Sports commentary</option><option value="business">Business / Marketing</option></select></label><label className="field-label">Translation style<select value={translationStyle} onChange={(e)=>setTranslationStyle(e.target.value)}><option value="neutral">Neutral spoken</option><option value="formal">Formal</option><option value="colloquial">Colloquial</option><option value="marketing">Marketing / energetic</option></select></label><p>Translation model: {config?.llm_model || 'configured LLM'}</p><p>Clone your voice after recording the live consent phrase. Original background sound is not kept.</p></details>
          <button className="primary-button" disabled={!video || !languages.length || busy} onClick={createDub}><WandSparkles size={17}/>{busy?'Uploading…':'Continue to voice setup'}</button>
          <p className="fine-print">Works best for single-speaker lessons, explainers, talks, tutorials, and solo podcasts. Multi-speaker and music-heavy videos are not supported.</p>
        </div>
        <div className="project-list"><div className="section-title"><h3>Recent projects</h3><span>{projects.length}</span></div>{projects.length ? projects.map((item)=><button className="project-row" key={item.id} onClick={()=>openProject(item)}><span className="file-icon"><FileVideo size={18}/></span><span><strong>{item.name}</strong><small>{item.status} · {item.duration_s ? `${item.duration_s.toFixed(1)} seconds`: 'No video yet'}</small></span><ChevronDown className="rotate" size={17}/></button>) : <div className="empty-state"><span className="empty-art"><Languages size={25}/></span><strong>Your verified dubs will live here</strong><p>Start with a short speech-led video.</p></div>}</div>
      </section> : <section className="workspace">
        {project.status==='uploaded' || project.status==='created' ? <div className="card voice-card"><div className="card-heading"><span className="icon-tile"><Mic size={18}/></span><div><h3>Set up your voice</h3><p>Record the consent phrase live, then provide 20–30 seconds of clean speech.</p></div></div>{estimate&&<div className="info-pill"><AudioLines size={15}/> Estimated credits: {estimate.estimated_credits} · {estimate.languages} languages · {estimate.remaining_daily_credits} left today</div>}<div className="consent-box"><span className="step-number">1</span><div><strong>Live consent recording</strong><p>{consentPhrase || 'Get a random phrase, then record yourself reading it.'}</p><small>This confirms a live recording; it does not prove identity.</small></div></div><div className="button-row"><button className="secondary-button" onClick={getConsentPhrase}>Get consent phrase</button>{recording?<button className="danger-button" onClick={stopConsentRecording}>Stop recording</button>:<button className="secondary-button" disabled={!consentPhrase} onClick={startConsentRecording}><Mic size={16}/> Record phrase</button>}{consentBlob&&<span className="ok-label"><Check size={15}/> Recorded</span>}</div><form onSubmit={saveVoice}><label className="field-label">Voice label<input name="label" defaultValue="My voice" /></label><label className="field-label">Clean reference sample<input name="reference_audio" type="file" accept="audio/wav,audio/mpeg,audio/*" required /></label><button className="primary-button" disabled={!consentBlob || busy}><ShieldCheck size={17}/>{busy?'Starting…':'Confirm consent and start'}</button></form></div> : <>
          <div className="language-tabs">{project.target_langs.map((lang)=><button className={activeLang===lang?'selected':''} onClick={()=>changeLanguage(lang)} key={lang}>{config?.languages?.[lang]||lang}<small>machine-checked only</small></button>)}</div>
          <div className="studio-grid"><section className="video-panel"><div className="section-title"><h3>Compare</h3><button className={compare?'toggle on':'toggle'} onClick={()=>setCompare(!compare)}>{compare?'Side by side':'Switch original / dub'}</button></div><video controls src={mediaUrl || undefined} /><div className="compare-video"><button className="secondary-button" onClick={()=>setMediaUrl(`/api/projects/${project.id}/video`)}>Original</button><button className="secondary-button" onClick={()=>setMediaUrl(`/api/projects/${project.id}/media?lang=${activeLang}`)}>Dubbed · AI dubbed</button></div><div className="status-counts"><span className="green">{counts.green||0} green</span><span className="yellow">{counts.yellow||0} yellow</span><span className="red">{counts.red||0} red</span></div><button className="primary-button" onClick={exportFiles}><FileAudio size={16}/> Export AI dubbed files</button><button className="text-button danger-text" onClick={deleteProject}><Trash2 size={15}/> Delete project and uploads</button></section>
            <section className="clips-panel"><details className="terms-drawer"><summary>Term sheet · {terms.length} protected terms</summary><div className="term-table">{terms.map((term,index)=><div className="term-row" key={term.id||term.term}><span><strong>{term.term}</strong><small>{term.type}</small></span><select value={term.treatment} onChange={(e)=>setTerms((items)=>items.map((item,i)=>i===index?{...item,treatment:e.target.value}:item))}><option value="keep">Keep</option><option value="transliterate">Transliterate</option><option value="translate">Translate</option></select><input aria-label={`Target form for ${term.term}`} value={term.targets?.[activeLang]||''} onChange={(e)=>setTerms((items)=>items.map((item,i)=>i===index?{...item,targets:{...item.targets,[activeLang]:e.target.value}}:item))}/><label><input type="checkbox" checked={term.locked} onChange={(e)=>setTerms((items)=>items.map((item,i)=>i===index?{...item,locked:e.target.checked}:item))}/> Lock</label></div>)}<button className="secondary-button" onClick={saveTerms}>Save term sheet</button></div></details><div className="section-title"><div><h3>Clips</h3><small>{jobMessage || 'Term and number checks on every line'}</small></div><button className="secondary-button" onClick={()=>document.querySelector<HTMLElement>('.clip-row.problem')?.scrollIntoView({behavior:'smooth',block:'center'})}>Next problem</button></div>{clips.length?clips.map((clip)=><article className={`clip-row ${clip.status||'pending'}`} key={clip.id}><div className="clip-meta"><span>{clip.start_s.toFixed(1)}s — {clip.end_s.toFixed(1)}s</span><span className={`badge ${clip.status||'pending'}`}>{clip.status||'pending'}</span></div><label className="field-label">Source text<textarea readOnly value={clip.src_text}/></label><label className="field-label">Translation<textarea defaultValue={clip.translation||''} key={`${clip.id}-${activeLang}-${clip.translation}`} onBlur={(e)=>saveTranslation(clip,e.target.value)}/></label>{clip.reasons?.map((reason)=><p className="reason" key={reason}>{reason}</p>)}<details><summary>Attempt history</summary><pre>{JSON.stringify(clip.attempts||[],null,2)}</pre></details><div className="button-row"><button className="secondary-button" onClick={()=>request(`/clips/${clip.id}/dubs/${activeLang}/regenerate`,{method:'POST'}).then(()=>setNotice('Regeneration queued.'))}>Regenerate</button><button className="secondary-button" onClick={()=>request(`/clips/${clip.id}/dubs/${activeLang}/revert`,{method:'POST'}).then(()=>changeLanguage(activeLang))}>Revert</button></div></article>):<div className="empty-state"><span className="empty-art"><AudioLines size={24}/></span><strong>{jobMessage||'Waiting for the first clips'}</strong><p>Processing progress appears here.</p></div>}</section></div></>}
      </section>}
      <footer>Built on Gnani AI (Prisma, voice cloning) <span>·</span> Anek is an independent project <button className="text-button" onClick={deleteAllData}>Delete my data</button></footer>
    </main>
  </div>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
