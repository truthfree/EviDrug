import react from '@vitejs/plugin-react'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { defineConfig } from 'vite'

type PackageMetadata = {
  version: string
}

const packageMetadata = JSON.parse(
  readFileSync(resolve(import.meta.dirname, 'package.json'), 'utf8'),
) as PackageMetadata

function resolveCommitSha() {
  const deploymentCommit =
    process.env.VITE_GIT_COMMIT ??
    process.env.VERCEL_GIT_COMMIT_SHA ??
    process.env.GITHUB_SHA ??
    process.env.RENDER_GIT_COMMIT

  if (deploymentCommit) {
    return deploymentCommit
  }

  try {
    return execFileSync('git', ['rev-parse', 'HEAD'], {
      cwd: import.meta.dirname,
      encoding: 'utf8',
    }).trim()
  } catch {
    return 'unknown'
  }
}

// https://vite.dev/config/
export default defineConfig({
  define: {
    'import.meta.env.VITE_APP_VERSION': JSON.stringify(packageMetadata.version),
    'import.meta.env.VITE_GIT_COMMIT': JSON.stringify(resolveCommitSha()),
  },
  plugins: [react()],
  server: {
    host: true,
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
