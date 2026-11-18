// The capture buffer's cap. The records wait here for the next flush; past the cap the oldest go.
// The cap is a parameter (default MAX_BUFFER) so a test can use a small one instead of 2,400 hook calls
// that outrun the engine's 10 s hook budget on a loaded machine.

export const MAX_BUFFER = 2000

// A cap below 1, or one that is not a whole number (NaN, Infinity), would keep one record on push, none on
// requeue, or never trim: refuse it before the buffer is touched.
function checkCap(cap: number): void {
  if (!Number.isInteger(cap) || cap < 1) throw new RangeError(`buffer cap must be a positive integer, got ${cap}`)
}

// Adds one record, dropping the oldest first when the buffer is full.
export function pushCapped<T>(buffer: T[], item: T, cap: number = MAX_BUFFER): void {
  checkCap(cap)
  if (buffer.length >= cap) buffer.shift()
  buffer.push(item)
}

// Puts a failed batch back in front of what arrived meanwhile (same order); the newest `cap` stay.
export function requeueCapped<T>(buffer: T[], batch: T[], cap: number = MAX_BUFFER): void {
  checkCap(cap)
  buffer.unshift(...batch)
  if (buffer.length > cap) buffer.splice(0, buffer.length - cap)
}
