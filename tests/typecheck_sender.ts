import type { ClientMessage, CommandRequest, SendCommand } from '../web/src/generated/protocol'
import type { Session } from '../web/src/session'
import type { DocumentFiles, DocumentFilesDeps } from '../web/src/documentFiles.svelte'
import { commandRequest, type CommandDef } from '../web/src/commands/command'

declare const session: Session
declare const send: SendCommand
declare const files: DocumentFiles
declare const deps: DocumentFilesDeps
declare const acceptMessage: (message: ClientMessage) => void
declare const acceptRequest: (...request: CommandRequest) => void

session.sendCommand('fillet', { radius: 2 }, 1)
session.sendCommand('measure', {}, 2)
session.sendCommand('ai_set_api_key', { key: null }, 3)
send('run_optimization', { element_size_mm: null, min_width_mm: null, debug: ['space'] }, 4)
files.run('export_stl', { path: 'part.stl' })
deps.sendCommand('save_thumbnail', { name: 'part.json', png: '' }, 5)
acceptMessage({ cmd: 'undo', client_op: 1, ack_id: 2 })
acceptMessage({ cmd: 'preview', verb: 'fillet', args: { radius: 1 }, preview_id: 3 })
acceptMessage({ cmd: 'set_view_resolution', world_per_pixel: 1, focus: null })
acceptMessage({ cmd: 'finish_optimization', step: 0, debug: ['space'] })
acceptMessage({ cmd: 'optimize_step_voxels', index: 0 })
acceptRequest(...commandRequest(['add_circle', { center: [0, 0], diameter: 2 }], 'sketch-1'))

// @ts-expect-error unknown command
session.sendCommand('not_a_command', {}, 1)
// @ts-expect-error required argument
session.sendCommand('fillet', {}, 1)
// @ts-expect-error wrong value type
session.sendCommand('fillet', { radius: '2' }, 1)
// @ts-expect-error arguments belong to another command
session.sendCommand('fillet', { distance: 2 }, 1)
// @ts-expect-error metadata is not a command argument
session.sendCommand('undo', { ack_id: 1 }, 1)
// @ts-expect-error client operation must be a number
session.sendCommand('measure', {}, '1')
// @ts-expect-error command takes no arguments
files.run('list_documents', { name: 'part.json' })
// @ts-expect-error file dependency preserves the sender signature
deps.sendCommand('open_document', {}, 1)
// @ts-expect-error missing required field in the low-level envelope
acceptMessage({ cmd: 'set_visible', body_id: 'body-1' })
// @ts-expect-error required number cannot be null
acceptMessage({ cmd: 'set_view_resolution', world_per_pixel: null })
// @ts-expect-error acknowledgement id must be a number
acceptMessage({ cmd: 'undo', ack_id: '1' })
// @ts-expect-error command-specific field on another command
acceptMessage({ cmd: 'get_edges', body_id: 'body-1', index: 0 })
// @ts-expect-error required voxel step
acceptMessage({ cmd: 'optimize_step_voxels' })
// @ts-expect-error typed build request requires its own arguments
commandRequest(['add_circle', { radius: 2 }], 'sketch-1')

declare const unrelatedName: 'fillet' | 'extrude'
// @ts-expect-error a union of names does not prove which arguments belong to the chosen command
send(unrelatedName, { radius: 2 }, 1)

// @ts-expect-error a general command definition must also correlate id and build result
const mismatchedDefinition: CommandDef = { id: 'fillet', label: '', buttonId: '', args: [], preview: 'none',
  build: () => ({ ok: true, args: { distance: 2 } }) }
void mismatchedDefinition
