// 語音輸入的「蘋果內建辨識」小幫手（用 macOS 26 的 SpeechTranscriber，免費、在本機跑）。
//
// 主程式會讓它一直在背景待命：
//   啟動後先回一行 {"ready": true}（或 {"error": "..."}）
//   之後每收到一行 {"path": "錄音.wav"}，就回一行 {"text": "..."} 或 {"error": "..."}
// 用法：apple-speech [語言，預設 zh-TW]

import AVFoundation
import Foundation
import Speech

func reply(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object) else { return }
    FileHandle.standardOutput.write(data + Data("\n".utf8))
}

@available(macOS 26.0, *)
func prepare(locale: Locale) async throws {
    let supported = await SpeechTranscriber.supportedLocales.map { $0.identifier(.bcp47) }
    guard supported.contains(locale.identifier(.bcp47)) else {
        throw NSError(domain: "apple-speech", code: 1, userInfo: [NSLocalizedDescriptionKey: "不支援 \(locale.identifier)"])
    }
    // 語言包還沒下載的話，向蘋果下載（免費，只需要一次）
    let transcriber = SpeechTranscriber(locale: locale, preset: .transcription)
    if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
        try await request.downloadAndInstall()
    }
}

@available(macOS 26.0, *)
func transcribe(path: String, locale: Locale) async throws -> String {
    let transcriber = SpeechTranscriber(locale: locale, preset: .transcription)
    let analyzer = SpeechAnalyzer(modules: [transcriber])
    let file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
    async let text = transcriber.results.reduce("") { $0 + String($1.text.characters) }
    if let last = try await analyzer.analyzeSequence(from: file) {
        try await analyzer.finalizeAndFinish(through: last)
    } else {
        await analyzer.cancelAndFinishNow()
    }
    return try await text
}

/// 在背景跑 async 工作，等它做完再繼續（這個小程式一次只處理一件事）
func runAndWait(_ work: @escaping () async -> Void) {
    let done = DispatchSemaphore(value: 0)
    Task {
        await work()
        done.signal()
    }
    done.wait()
}

guard #available(macOS 26.0, *) else {
    reply(["error": "需要 macOS 26 以上"])
    exit(1)
}

let locale = Locale(identifier: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "zh-TW")
var ready = false
runAndWait {
    do {
        try await prepare(locale: locale)
        ready = true
        reply(["ready": true])
    } catch {
        reply(["error": error.localizedDescription])
    }
}
if !ready { exit(1) }

while let line = readLine() {
    guard let data = line.data(using: .utf8),
          let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let path = request["path"] as? String
    else {
        reply(["error": "看不懂的指令"])
        continue
    }
    runAndWait {
        do {
            reply(["text": try await transcribe(path: path, locale: locale)])
        } catch {
            reply(["error": error.localizedDescription])
        }
    }
}
