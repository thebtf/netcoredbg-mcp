import fs from 'fs';
import path from 'path';

const logsDir = 'C:/Users/btf/.claude/projects/D--Dev-novascript';
const files = fs.readdirSync(logsDir)
  .filter(f => f.endsWith('.jsonl') && !f.startsWith('agent-'))
  .map(f => ({ name: f, size: fs.statSync(path.join(logsDir, f)).size }))
  .filter(f => f.size > 50000)
  .sort((a, b) => b.size - a.size)
  .slice(0, 15);

console.log('Analyzing', files.length, 'largest log files...\n');

let prompts = [];

for (const file of files) {
  const content = fs.readFileSync(path.join(logsDir, file.name), 'utf-8');
  const lines = content.split('\n');

  for (const line of lines) {
    if (!line.includes('"type":"user"')) continue;

    try {
      const obj = JSON.parse(line);
      if (obj.message && obj.message.content) {
        for (const item of obj.message.content) {
          if (item.type === 'text' && item.text) {
            const text = item.text.trim();
            // Filter out system/IDE messages
            if (text.startsWith('<ide_') ||
                text.startsWith('Todos have been') ||
                text.includes('tool_result') ||
                text.length < 10 ||
                text.length > 2000) continue;

            prompts.push({
              text: text,
              timestamp: obj.timestamp,
              branch: obj.gitBranch
            });
          }
        }
      }
    } catch(e) {}
  }
}

// Deduplicate
const seen = new Set();
const unique = prompts.filter(p => {
  const key = p.text.substring(0, 100);
  if (seen.has(key)) return false;
  seen.add(key);
  return true;
});

console.log('Found', unique.length, 'unique user prompts\n');
console.log('=== SAMPLE PROMPTS (last 100) ===\n');

unique
  .sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp))
  .slice(0, 100)
  .forEach((p, i) => {
    const preview = p.text.replace(/\n/g, ' ').substring(0, 150);
    console.log(`${i + 1}. [${p.branch || 'no-branch'}] ${preview}...`);
  });

// Analyze patterns
console.log('\n\n=== PATTERN ANALYSIS ===\n');

// Language detection
const russian = unique.filter(p => /[а-яё]/i.test(p.text)).length;
const english = unique.length - russian;
console.log(`Language: ${russian} Russian (${Math.round(russian/unique.length*100)}%), ${english} English`);

// Common starting words
const starts = {};
unique.forEach(p => {
  const words = p.text.toLowerCase().split(/\s+/).slice(0, 2).join(' ');
  starts[words] = (starts[words] || 0) + 1;
});

console.log('\nCommon prompt starts:');
Object.entries(starts)
  .sort((a, b) => b[1] - a[1])
  .slice(0, 30)
  .forEach(([word, count]) => console.log(`  "${word}": ${count}`));

// Keywords frequency
const keywords = {};
const importantWords = ['сделай', 'добавь', 'исправь', 'проверь', 'напиши', 'создай', 'удали', 'обнови', 'найди', 'покажи',
  'fix', 'add', 'create', 'remove', 'update', 'check', 'find', 'show', 'implement', 'refactor',
  'debug', 'test', 'build', 'commit', 'push', 'merge', 'review', 'analyze', 'plan', 'design'];

unique.forEach(p => {
  const text = p.text.toLowerCase();
  importantWords.forEach(kw => {
    if (text.includes(kw)) {
      keywords[kw] = (keywords[kw] || 0) + 1;
    }
  });
});

console.log('\nAction keywords frequency:');
Object.entries(keywords)
  .sort((a, b) => b[1] - a[1])
  .forEach(([word, count]) => console.log(`  ${word}: ${count}`));

// Prompt length distribution
const lengths = unique.map(p => p.text.length);
const avgLen = Math.round(lengths.reduce((a, b) => a + b, 0) / lengths.length);
const short = lengths.filter(l => l < 50).length;
const medium = lengths.filter(l => l >= 50 && l < 200).length;
const long = lengths.filter(l => l >= 200).length;

console.log(`\nPrompt length: avg ${avgLen} chars`);
console.log(`  Short (<50): ${short} (${Math.round(short/unique.length*100)}%)`);
console.log(`  Medium (50-200): ${medium} (${Math.round(medium/unique.length*100)}%)`);
console.log(`  Long (>200): ${long} (${Math.round(long/unique.length*100)}%)`);
