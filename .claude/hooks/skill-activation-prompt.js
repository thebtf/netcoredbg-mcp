#!/usr/bin/env node
import { readFileSync } from 'fs';
import { join } from 'path';
async function main() {
    try {
        // Read input from stdin
        const input = readFileSync(0, 'utf-8');
        const data = JSON.parse(input);
        const prompt = data.prompt.toLowerCase();
        // Load skill rules from .agent/skills/ (NovaScript convention)
        const projectDir = process.env.CLAUDE_PROJECT_DIR || process.cwd();
        const rulesPath = join(projectDir, '.agent', 'skills', 'skill-rules.json');
        let rules;
        try {
            rules = JSON.parse(readFileSync(rulesPath, 'utf-8'));
        }
        catch {
            // No skill rules file - exit silently
            process.exit(0);
        }
        const matchedSkills = [];
        // Check each skill for matches
        for (const [skillName, config] of Object.entries(rules.skills)) {
            const triggers = config.promptTriggers;
            if (!triggers) {
                continue;
            }
            // Keyword matching
            if (triggers.keywords) {
                const keywordMatch = triggers.keywords.some(kw => prompt.includes(kw.toLowerCase()));
                if (keywordMatch) {
                    matchedSkills.push({ name: skillName, matchType: 'keyword', config });
                    continue;
                }
            }
            // Intent pattern matching with ReDoS protection
            if (triggers.intentPatterns) {
                const intentMatch = triggers.intentPatterns.some(pattern => {
                    // Validate regex pattern: reject dangerous constructs
                    if (/(\.\*){2,}|(\.\+){2,}|(\.[*+]){3,}/.test(pattern)) {
                        console.error(`Skipping dangerous regex pattern: ${pattern}`);
                        return false;
                    }
                    try {
                        const regex = new RegExp(pattern, 'i');
                        return regex.test(prompt);
                    }
                    catch {
                        console.error(`Invalid regex pattern: ${pattern}`);
                        return false;
                    }
                });
                if (intentMatch) {
                    matchedSkills.push({ name: skillName, matchType: 'intent', config });
                }
            }
        }
        // Generate output if matches found
        if (matchedSkills.length > 0) {
            let output = '\n';
            output += '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n';
            output += '🎯 SKILL ACTIVATION\n';
            output += '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n';
            // Group by priority
            const critical = matchedSkills.filter(s => s.config.priority === 'critical');
            const high = matchedSkills.filter(s => s.config.priority === 'high');
            const medium = matchedSkills.filter(s => s.config.priority === 'medium');
            const low = matchedSkills.filter(s => s.config.priority === 'low');
            if (critical.length > 0) {
                output += '⚠️ CRITICAL SKILLS (REQUIRED):\n';
                critical.forEach(s => {
                    output += `  → ${s.name}`;
                    if (s.config.description)
                        output += ` - ${s.config.description}`;
                    output += '\n';
                });
                output += '\n';
            }
            if (high.length > 0) {
                output += '📚 RECOMMENDED SKILLS:\n';
                high.forEach(s => {
                    output += `  → ${s.name}`;
                    if (s.config.description)
                        output += ` - ${s.config.description}`;
                    output += '\n';
                });
                output += '\n';
            }
            if (medium.length > 0) {
                output += '💡 SUGGESTED SKILLS:\n';
                medium.forEach(s => {
                    output += `  → ${s.name}`;
                    if (s.config.description)
                        output += ` - ${s.config.description}`;
                    output += '\n';
                });
                output += '\n';
            }
            if (low.length > 0) {
                output += '📌 OPTIONAL SKILLS:\n';
                low.forEach(s => {
                    output += `  → ${s.name}`;
                    if (s.config.description)
                        output += ` - ${s.config.description}`;
                    output += '\n';
                });
                output += '\n';
            }
            const skillNames = matchedSkills.map(s => s.name).join(', ');
            output += `Read skill file(s): ${skillNames} → .agent/skills/<name>/SKILL.md\n`;
            output += '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n';
            console.log(output);
        }
        process.exit(0);
    }
    catch (err) {
        // Log error for debugging but don't break user workflow
        if (process.env.DEBUG) {
            console.error('[skill-activation-prompt] Error:', err);
        }
        process.exit(0);
    }
}
main().catch(() => {
    process.exit(0);
});
