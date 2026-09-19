import test from 'node:test';
import assert from 'node:assert/strict';
import { segments } from '../src/segments.ts';
const clip=(start,state='ready',duration=15)=>({id:String(start),start,state,duration,url:null,error:''});
test('provider jobs combine into 30-second viewing segments',()=>{
 const result=segments([clip(0),clip(15),clip(30),clip(45,'generating')]);
 assert.equal(result.length,2);
 assert.deepEqual(result.map(s=>[s.start,s.duration,s.state]),[[0,30,'ready'],[30,30,'generating']]);
});
test('failed segment retains successful jobs for a targeted retry',()=>{
 const result=segments([clip(0),clip(15,'error')]);
 assert.equal(result[0].state,'error');
 assert.deepEqual(result[0].chunks.filter(c=>c.state==='error').map(c=>c.id),['15']);
});
test('unknown submission is visible as review, not retriable error',()=>{
 assert.equal(segments([clip(90,'review'),clip(105,'queued')])[0].state,'review');
});
test('a provider job across a boundary still produces a 30-second viewing scale',()=>{
 const result=segments([clip(0,'ready',28),clip(28,'ready',4),clip(32,'generating',28)]);
 assert.deepEqual(result.map(s=>[s.start,s.duration,s.state]),[[0,30,'ready'],[30,30,'generating']]);
});
