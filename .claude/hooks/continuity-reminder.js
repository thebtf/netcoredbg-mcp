#!/usr/bin/env node
/**
 * Continuity Reminder Hook (PreCompact)
 *
 * Runs BEFORE context compression to remind agent to save state.
 * This is the critical moment to persist work before context is lost.
 *
 * Determines role from worktree path:
 * - docwriter -> CONTINUITY-DOCWRITER.md
 * - integrator -> CONTINUITY-INTEGRATOR.md
 * - main repo -> CONTINUITY-CODER.md
 */
import { existsSync } from 'fs';
import { join, basename } from 'path';

function main() {
    try {
        const projectDir = process.env.CLAUDE_PROJECT_DIR || process.cwd();

        // Determine role from path
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

        // Urgent message - context is about to be compressed!
        let output = '\n';
        output += '╔═══════════════════════════════════════════════════════════╗\n';
        output += '║  ⚠️  CONTEXT COMPRESSION IMMINENT                         ║\n';
        output += '╠═══════════════════════════════════════════════════════════╣\n';
        output += `║  Role: ${role.padEnd(50)}║\n`;
        output += `║  File: .agent/${continuityFile.padEnd(42)}║\n`;
        output += '╠═══════════════════════════════════════════════════════════╣\n';

        if (exists) {
            output += '║  🔴 MANDATORY BEFORE COMPRESSION:                         ║\n';
            output += '║     Update CONTINUITY file with current state:            ║\n';
            output += '║     - What was done (Done section)                        ║\n';
            output += '║     - Current task (Now section)                          ║\n';
            output += '║     - Next steps (Next section)                           ║\n';
            output += '║     - Open questions                                      ║\n';
        } else {
            output += '║  🔴 CRITICAL: CONTINUITY FILE MISSING!                    ║\n';
            output += '║     Create it NOW before context is lost!                 ║\n';
            output += '║     Template in AGENTS.md -> Continuity Ledger section    ║\n';
        }

        output += '╚═══════════════════════════════════════════════════════════╝\n';

        console.log(output);
        process.exit(0);
    } catch (err) {
        if (process.env.DEBUG) {
            console.error('[continuity-reminder] Error:', err);
        }
        process.exit(0);
    }
}

main();
