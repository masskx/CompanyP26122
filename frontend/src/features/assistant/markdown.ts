import DOMPurify from 'dompurify'
import { marked } from 'marked'

const allowedTags = [
  'a',
  'blockquote',
  'br',
  'code',
  'del',
  'em',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'hr',
  'li',
  'ol',
  'p',
  'pre',
  'strong',
  'table',
  'tbody',
  'td',
  'th',
  'thead',
  'tr',
  'ul',
]

export function renderMarkdown(source: string): string {
  if (!source.trim()) {
    return ''
  }

  const rendered = marked.parse(source, {
    async: false,
    breaks: true,
    gfm: true,
  })
  const html = typeof rendered === 'string' ? rendered : ''
  const sanitized = DOMPurify.sanitize(html, {
    ALLOWED_TAGS: allowedTags,
    ALLOWED_ATTR: ['href', 'title'],
    ALLOW_DATA_ATTR: false,
    FORBID_TAGS: ['img', 'script', 'style'],
    FORBID_ATTR: ['style'],
  })

  const template = document.createElement('template')
  template.innerHTML = sanitized
  template.content.querySelectorAll('a').forEach((link) => {
    link.setAttribute('target', '_blank')
    link.setAttribute('rel', 'noopener noreferrer')
  })
  return template.innerHTML
}
