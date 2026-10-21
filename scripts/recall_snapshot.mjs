#!/usr/bin/env node
/** Offline canonical projection bridge. Stdout contains private text: pipe to a 0600 file. */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import readline from 'node:readline';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';

const hash = x => createHash('sha256').update(typeof x === 'string' ? x : JSON.stringify(x)).digest('hex');
function need(ok, message) { if (!ok) throw new Error(message); }
function installation() {
  const root = path.join(os.homedir(), '.pi/agent/install');
  const version = fs.readFileSync(path.join(root, 'current-version'), 'utf8').trim();
  need(/^\d+\.\d+\.\d+(?:[-.\w]*)?$/.test(version), 'invalid managed Pi version');
  return path.join(root, 'releases', version, 'node_modules/@earendil-works/pi-coding-agent');
}
function lineage(entries, leafId) {
  const byId = new Map();
  for (const e of entries) {
    need(typeof e.id === 'string' && e.id.length > 0, 'entry without id');
    need(!byId.has(e.id), 'ambiguous duplicate entry id');
    byId.set(e.id, e);
  }
  const out = [], seen = new Set();
  let id = leafId;
  while (id !== null) {
    need(typeof id === 'string' && byId.has(id), 'missing lineage parent');
    need(!seen.has(id), 'lineage cycle'); seen.add(id);
    const e = byId.get(id); out.push(e); id = e.parentId;
  }
  return out.reverse();
}
function parts(message) {
  const result = [], excluded = [];
  const content = message.content;
  if (typeof content === 'string') result.push({type:'text', part_index:null, text:content});
  else if (Array.isArray(content)) for (const [i,p] of content.entries()) {
    if (p.type === 'text' && typeof p.text === 'string') result.push({type:'text', part_index:i, text:p.text});
    else if (p.type === 'thinking' && typeof p.thinking === 'string' && !p.redacted) {
      result.push({type:'thinking', part_index:i, text:p.thinking});
    } else if (p.type === 'toolCall') {
      result.push({type:'toolCall', part_index:i, text:JSON.stringify(p.arguments ?? {}), name:p.name, call_id:p.id});
    } else excluded.push({part_index:i,type:p.type ?? 'unknown',reason:'non-text-or-redacted'});
    if (p.thinkingSignature || p.thoughtSignature || p.textSignature) excluded.push({part_index:i,type:'signature',reason:'opaque-not-scored'});
  }
  if (message.role === 'bashExecution') {
    result.push({type:'bash',part_index:null,text:`$ ${message.command ?? ''}\n${message.output ?? ''}`});
  }
  return {parts:result, excluded};
}
function convert(projected, globals, edits = new Map()) {
  const e=projected.sourceEntry;
  const edit=edits.get(e.id);
  return projected.messages.map(m => ({
    entry_id:e.id, ref:globals.get(e.id) ?? null, source_type:e.type,
    role:m.role, timestamp:e.timestamp, tool_name:m.toolName ?? null,
    tool_call_id:m.toolCallId ?? null, exclude_from_context:m.excludeFromContext === true,
    raw_entry_sha256:hash(e), projected_message_sha256:hash(m),
    origin:edit ? {kind:'context_edit',entry_id:edit.id,target_id:e.id,raw_entry_sha256:hash(edit)} : {kind:'message',entry_id:e.id},
    ...parts(m),
    ...(typeof m.summary === 'string' ? {summary:m.summary}:{}),
  }));
}
export async function snapshot({session,compaction,piPackage,vccPackage,maxBytes=512*1024*1024}) {
  need(typeof session === 'string' && typeof compaction === 'string', 'session and compaction required');
  piPackage ??= installation();
  vccPackage ??= path.join(os.homedir(), '.pi/agent/npm/node_modules/@sting8k/pi-vcc');
  const piVersion=JSON.parse(fs.readFileSync(path.join(piPackage,'package.json'),'utf8')).version;
  const vccVersion=JSON.parse(fs.readFileSync(path.join(vccPackage,'package.json'),'utf8')).version;
  need(vccVersion === '0.10.2', 'unsupported pi-vcc version: revalidate source contract');
  const {buildSessionProjection}=await import(pathToFileURL(path.join(piPackage,'dist/core/session-manager.js')).href);
  const jitiPath=path.resolve(piPackage,'../../jiti/lib/jiti.mjs');
  const {createJiti}=await import(pathToFileURL(jitiPath).href);
  const jiti=createJiti(import.meta.url,{fsCache:false,moduleCache:false});
  const {buildGlobalIndexById}=await jiti.import(path.join(vccPackage,'src/core/global-indices.ts'));
  const input=fs.createReadStream(session);
  const lines=readline.createInterface({input,crlfDelay:Infinity});
  const entries=[], digest=createHash('sha256'); let header=null, cutoff=null, bytes=0, lineCount=0;
  try {
    for await (const line of lines) {
      lineCount++; bytes+=Buffer.byteLength(line,'utf8')+1;
      need(bytes<=maxBytes,'snapshot input byte budget exceeded');
      if (!line.trim()) continue;
      let e; try { e=JSON.parse(line); } catch { throw new Error(`malformed JSONL at line ${lineCount}`); }
      digest.update(JSON.stringify(e)+'\n');
      if (e.type === 'session') { need(header===null,'multiple session headers'); header=e; continue; }
      entries.push(e);
      if (e.id === compaction) { need(e.type==='compaction','target is not a compaction'); cutoff=e; break; }
    }
  } finally { lines.close(); input.destroy(); }
  need(header && cutoff,'session header or compaction not found');
  need(cutoff.details?.compactor === 'pi-vcc','target was not produced by pi-vcc');
  const branch=lineage(entries,cutoff.id);
  const global=buildGlobalIndexById(entries);
  // Canonical before/after set difference, not inclusive integer-range arithmetic.
  const before=buildSessionProjection(entries,cutoff.parentId);
  const after=buildSessionProjection(entries,cutoff.id);
  const keptIds=new Set(after.entries.filter(e=>e.messages.length).map(e=>e.sourceEntry.id));
  const dropped=before.entries.filter(e=>e.messages.length && !keptIds.has(e.sourceEntry.id));
  const editsOf=projection=>new Map(projection.entries.filter(e=>e.sourceEntry.type==='context_edit').map(e=>[e.sourceEntry.targetId,e.sourceEntry]));
  const droppedMessages=dropped.flatMap(e=>convert(e,global,editsOf(before)));
  const currentMessages=after.entries.flatMap(e=>convert(e,global,editsOf(after)));
  const excluded=[];
  const candidateMessages=[];
  for (const m of droppedMessages) {
    if (m.ref===null || m.role==='system' || m.exclude_from_context) {
      excluded.push({entry_id:m.entry_id,ref:m.ref,role:m.role,reason:m.ref===null?'not-a-global-message-ref':m.role==='system'?'system-not-conversation':'excluded-bash'});
    } else candidateMessages.push(m);
  }
  return {schema:1,session_id:header.id,session_path:path.resolve(session),compaction_id:cutoff.id,
    compaction_timestamp:cutoff.timestamp,first_kept_entry_id:cutoff.firstKeptEntryId,
    source_prefix_sha256:digest.digest('hex'),source_prefix_lines:lineCount,
    source_prefix_normalized_bytes:bytes,projection_engine:{pi:piVersion,vcc:vccVersion,pi_package:piPackage,vcc_package:vccPackage},
    active_branch_ids:branch.map(e=>e.id),global_indices:Object.fromEntries(global),
    dropped_messages:candidateMessages,current_messages:currentMessages,excluded,
    coverage:{dropped_context_entries:dropped.length,candidate_messages:candidateMessages.length,excluded_messages:excluded.length},
  };
}
async function main() {
  const args=process.argv.slice(2), opts={};
  if(args.includes('--help')) {console.log('recall_snapshot.mjs --session FILE --compaction ID [--pi-package DIR] [--vcc-package DIR] [--max-bytes N]');return;}
  const keys={'--session':'session','--compaction':'compaction','--pi-package':'piPackage','--vcc-package':'vccPackage','--max-bytes':'maxBytes'};
  for(let i=0;i<args.length;i+=2) {need(keys[args[i]] && args[i+1], 'invalid arguments');opts[keys[args[i]]]=args[i+1];}
  if(opts.maxBytes!==undefined) {opts.maxBytes=Number(opts.maxBytes);need(Number.isSafeInteger(opts.maxBytes)&&opts.maxBytes>0,'invalid byte budget');}
  console.log(JSON.stringify(await snapshot(opts)));
}
if(process.argv[1] && path.resolve(process.argv[1])===path.resolve(new URL(import.meta.url).pathname)) main().catch(e=>{console.error(JSON.stringify({ok:false,error:e.message}));process.exitCode=1;});
