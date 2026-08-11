/**
 * Выносит исполняемые inline-скрипты из собранной статики в отдельные файлы.
 *
 * Nuxt вставляет в index.html скрипт с runtime-конфигом. Под требуемой
 * политикой `script-src 'self'` без unsafe-inline браузер его блокирует,
 * и приложение просто не стартует. Вариант «разрешить хеш» пришлось бы
 * пересчитывать на каждую сборку (в конфиге лежит buildId), поэтому скрипт
 * выносится в файл — политика остаётся строгой и не зависит от сборки.
 *
 * Блоки <script type="application/json"> не трогаем: браузер их не исполняет,
 * CSP к ним не применяется.
 */

import { createHash } from 'node:crypto'
import { readdir, readFile, writeFile } from 'node:fs/promises'
import { join, relative } from 'node:path'

const ROOT = new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')
const PUBLIC_DIR = join(ROOT, '.output', 'public')
const ASSETS_DIR = join(PUBLIC_DIR, '_nuxt')

const INLINE_SCRIPT = /<script(?![^>]*\bsrc=)([^>]*)>([\s\S]*?)<\/script>/g

async function htmlFiles(dir) {
  const found = []
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) found.push(...(await htmlFiles(path)))
    else if (entry.name.endsWith('.html')) found.push(path)
  }
  return found
}

function isExecutable(attributes) {
  const type = /type=["']([^"']+)["']/.exec(attributes)
  return !type || type[1] === 'module' || type[1].includes('javascript')
}

let moved = 0

for (const file of await htmlFiles(PUBLIC_DIR)) {
  const html = await readFile(file, 'utf8')
  const replacements = []

  for (const match of html.matchAll(INLINE_SCRIPT)) {
    const [full, attributes, code] = match
    if (!isExecutable(attributes) || !code.trim()) continue

    const hash = createHash('sha256').update(code).digest('hex').slice(0, 12)
    const name = `inline-${hash}.js`
    await writeFile(join(ASSETS_DIR, name), code, 'utf8')
    replacements.push([full, `<script src="/_nuxt/${name}"${attributes}></script>`])
    moved += 1
  }

  if (replacements.length) {
    let updated = html
    for (const [from, to] of replacements) updated = updated.replace(from, to)
    await writeFile(file, updated, 'utf8')
    console.log(`  ${relative(PUBLIC_DIR, file)}: вынесено скриптов — ${replacements.length}`)
  }
}

console.log(`Инлайновых скриптов вынесено в файлы: ${moved}`)
