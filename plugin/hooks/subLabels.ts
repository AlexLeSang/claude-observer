// The `subN` label of a subagent: one numbering per session, keyed on the agent id, in first-seen order.
// Shared by every line that names a subagent, so the same agent carries the same label everywhere. Pure
// data: the ids are opaque map keys and never part of a label. The snapshot is JSON, so a store can keep it.

const MAX_LABELS = 1000
const MAX_ID_CHARS = 200

type SubLabelSnapshot = { next: number; labels: [string, number][] }

export class SubLabels {
  private labels = new Map<string, number>()
  private nextNumber = 1

  constructor(snapshot?: unknown) {
    if (typeof snapshot !== 'object' || snapshot === null) return
    const { next, labels } = snapshot as { next?: unknown; labels?: unknown }
    let top = 0
    if (Array.isArray(labels)) {
      for (const row of labels.slice(0, MAX_LABELS)) {
        if (!Array.isArray(row) || typeof row[0] !== 'string' || row[0].length > MAX_ID_CHARS) continue
        const n = row[1]
        if (typeof n !== 'number' || !Number.isSafeInteger(n) || n < 1 || n > 1_000_000) continue
        if (this.labels.has(row[0]) || [...this.labels.values()].includes(n)) continue
        this.labels.set(row[0], n)
        top = Math.max(top, n)
      }
    }
    const wanted = typeof next === 'number' && Number.isSafeInteger(next) && next > 0 && next <= 1_000_001 ? next : 1
    this.nextNumber = Math.max(wanted, top + 1)
  }

  // The label of `id`, numbering it now when it is new. Undefined for a value that cannot be an id, and
  // once the numbering is full (a new id then has no label, rather than a repeated one).
  label(id: unknown): string | undefined {
    if (typeof id !== 'string' || id === '' || id.length > MAX_ID_CHARS) return undefined
    let n = this.labels.get(id)
    if (n === undefined) {
      if (this.labels.size >= MAX_LABELS) return undefined
      n = this.nextNumber++
      this.labels.set(id, n)
    }
    return `sub${n}`
  }

  snapshot(): SubLabelSnapshot {
    return { next: this.nextNumber, labels: [...this.labels] }
  }
}
