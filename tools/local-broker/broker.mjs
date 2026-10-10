// Local MQTT broker for testing the ESP32 water logger without a real server.
// Normally started with `dev.bat broker [-BrokerPort N]` from the repository root.
//
//   node broker.mjs                 listen on 0.0.0.0:1883 (PORT overrides), anyone may connect
//   MQTT_USER / MQTT_PASS           when set, clients must log in with these credentials
//
// Every message published by a client is printed and appended to messages.jsonl.

import fs from 'node:fs'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { Aedes } from 'aedes'

const PORT = Number(process.env.PORT || 1883)
const USER = process.env.MQTT_USER || ''
const PASS = process.env.MQTT_PASS || ''
const PREVIEW = 600 // characters of each payload printed to the console
const here = path.dirname(fileURLToPath(import.meta.url))
const logFile = path.join(here, 'messages.jsonl')

const stamp = () => new Date().toLocaleTimeString('vi-VN', { hour12: false })
const addressOf = (client) => client?.conn?.remoteAddress?.replace(/^::ffff:/, '') ?? '?'

function authenticate (client, username, password, callback) {
  if (!USER) return callback(null, true)
  const ok = username === USER && password?.toString() === PASS
  if (!ok) console.log(`${stamp()}  TỪ CHỐI  ${client.id} (${addressOf(client)}): sai username/password`)
  callback(null, ok)
}

function preview (payload) {
  const text = payload.toString('utf8')
  try {
    const compact = JSON.stringify(JSON.parse(text))
    return compact.length > PREVIEW ? `${compact.slice(0, PREVIEW)}… (${compact.length} ký tự)` : compact
  } catch {
    return text.length > PREVIEW ? `${text.slice(0, PREVIEW)}…` : text
  }
}

// The logger sends each device's records oldest first with seq rising by 1, so a jump
// means records were lost and a seq already seen is a resend (the server drops it).
const lastSeq = new Map()

function checkSequence (payload) {
  let record
  try {
    record = JSON.parse(payload.toString('utf8'))
  } catch {
    return ''
  }
  if (typeof record?.device_id !== 'string' || !Number.isInteger(record.seq)) return ''
  const last = lastSeq.get(record.device_id)
  if (last !== undefined && record.seq <= last) return `  seq ${record.seq} GỬI LẠI (đã nhận)`
  lastSeq.set(record.device_id, record.seq)
  if (last === undefined || record.seq === last + 1) return `  seq ${record.seq}`
  const missing = record.seq - 1 > last + 1 ? `${last + 1}-${record.seq - 1}` : `${last + 1}`
  return `  seq ${record.seq}  MẤT seq ${missing}`
}

const broker = await Aedes.createBroker({ authenticate })

broker.on('client', (client) => {
  console.log(`${stamp()}  KẾT NỐI  ${client.id} từ ${addressOf(client)}`)
})
broker.on('clientDisconnect', (client) => {
  console.log(`${stamp()}  NGẮT     ${client.id}`)
})
broker.on('clientError', (client, err) => {
  console.log(`${stamp()}  LỖI      ${client.id}: ${err.message}`)
})
broker.on('subscribe', (subscriptions, client) => {
  console.log(`${stamp()}  ĐĂNG KÝ  ${client.id}: ${subscriptions.map((s) => s.topic).join(', ')}`)
})
broker.on('publish', (packet, client) => {
  if (!client) return // the broker's own $SYS messages
  console.log(`${stamp()}  NHẬN     ${packet.topic}  qos=${packet.qos}  ${packet.payload.length} byte  từ ${client.id}` +
              checkSequence(packet.payload))
  console.log(`           ${preview(packet.payload)}`)
  const record = {
    received_at: new Date().toISOString(),
    client: client.id,
    topic: packet.topic,
    qos: packet.qos,
    payload: packet.payload.toString('utf8')
  }
  fs.appendFile(logFile, JSON.stringify(record) + '\n', (err) => {
    if (err) console.log(`Không ghi được ${logFile}: ${err.message}`)
  })
})

const server = net.createServer(broker.handle)
server.on('error', (err) => {
  console.error(err.code === 'EADDRINUSE'
    ? `Cổng ${PORT} đang bị chương trình khác dùng. Chọn cổng khác, ví dụ: dev.bat broker -BrokerPort 1884`
    : err.message)
  process.exit(1)
})
server.listen(PORT, '0.0.0.0', () => {
  console.log(`Broker MQTT đang chạy, cổng ${PORT}${USER ? `, yêu cầu user "${USER}"` : ', không yêu cầu mật khẩu'}.`)
  console.log('ESP32 dùng URI theo IP của card mạng cùng Wi-Fi với nó:')
  for (const [name, addresses] of Object.entries(os.networkInterfaces())) {
    for (const a of addresses ?? []) {
      if (a.family === 'IPv4' && !a.internal) console.log(`  mqtt://${a.address}:${PORT}   (${name})`)
    }
  }
  console.log(`Bản tin nhận được được ghi vào ${logFile}. Ctrl+C để dừng.\n`)
})

process.on('SIGINT', () => {
  console.log('\nĐang dừng broker…')
  server.close()
  broker.close(() => process.exit(0))
})
