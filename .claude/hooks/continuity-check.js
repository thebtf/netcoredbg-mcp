#!/usr/bin/env node
/**
 * Continuity Check Hook (UserPromptSubmit)
 *
 * Detects completion signals in user prompts and reminds agent to update continuity.
 * Triggers on: commit, push, merge, done, готово, выполнено, etc.
 */
import { readFileSync, existsSync } from 'fs';
import { join, basename } from 'path';

function main() {
    try {
        // Read input from stdin
        const input = readFileSync(0, 'utf-8');
        const data = JSON.parse(input);
        const prompt = data.prompt.toLowerCase();

        // Completion signal keywords (Russian + English)
        const completionSignals = [
            // Task completion
            'готово', 'выполнено', 'выполнен', 'сделано', 'сделал', 'закончил', 'завершил',
            'done', 'finished', 'completed', 'complete',
            // Git operations
            'commit', 'коммит', 'закоммить', 'закоммитил',
            'push', 'пуш', 'запушь', 'запушил', 'пушни',
            'merge', 'мердж', 'замержь', 'замержил', 'слей',
            // Verification requests (agent asking user to check)
            'проверь', 'проверяй', 'протестируй', 'потестируй',
            'запусти', 'запускай', 'попробуй',
            'check', 'test', 'verify', 'try',
            // Epic/task completion
            'epic готов', 'epic done', 'задача готова', 'task complete',
            'pr создан', 'pr ready', 'пулл реквест готов',
            // Session end signals
            'на сегодня всё', 'на сегодня все', 'enough for today',
            'заканчиваем', 'хватит', 'стоп', 'stop'
        ];

        // Check if prompt contains completion signals
        const hasCompletionSignal = completionSignals.some(signal => prompt.includes(signal));

        if (!hasCompletionSignal) {
            process.exit(0);
        }

        // Determine role from path
        const projectDir = process.env.CLAUDE_PROJECT_DIR || process.cwd();
        const dirName = basename(projectDir).toLowerCase();
        let role = 'CODER'; // default

        if (dirName === 'docwriter' || projectDir.includes('docwriter')) {
            role = 'DOCWRITER';
        } else if (dirName === 'integrator' || projectDir.includes('integrator')) {
            role = 'INTEGRATOR';
        } else if (dirName === 'architect' || projectDir.includes('architect')) {
            role = 'ARCHITECT';
        }

        const continuityFile = `CONTINUITY-${role}.md`;
        const continuityPath = join(projectDir, '.agent', continuityFile);
        const exists = existsSync(continuityPath);

        // Reminder message
        let output = '\n';
        output += '┌───────────────────────────────────────────────────────────┐\n';
        output += '│  📝 CONTINUITY REMINDER                                   │\n';
        output += '├───────────────────────────────────────────────────────────┤\n';
        output += `│  Role: ${role.padEnd(51)}│\n`;
        output += `│  File: .agent/${continuityFile.padEnd(43)}│\n`;
        output += '├───────────────────────────────────────────────────────────┤\n';

        if (exists) {
            output += '│  Before moving on, consider updating:                     │\n';
            output += '│  • Done: What was completed                               │\n';
            output += '│  • Now: Current focus (if continuing)                     │\n';
            output += '│  • Next: Upcoming tasks                                   │\n';
            output += '│  • Open questions: Any blockers discovered                │\n';
        } else {
            output += '│  ⚠️  CONTINUITY FILE MISSING!                             │\n';
            output += '│  Create it using template from AGENTS.md                  │\n';
        }

        output += '└───────────────────────────────────────────────────────────┘\n';

        console.log(output);
        process.exit(0);
    } catch (err) {
        if (process.env.DEBUG) {
            console.error('[continuity-check] Error:', err);
        }
        process.exit(0);
    }
}

main();
