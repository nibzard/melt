import Foundation

// The service validates the language. This file defines only the private host transport.
typealias Object = [String: Any]
func object(_ value: Any?) -> Object { value as? Object ?? [:] }
func array(_ value: Any?) -> [Any] { value as? [Any] ?? [] }
func string(_ value: Any?) -> String { value as? String ?? "" }

enum HostError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let text) = self { return text }; return nil }
}

struct LocalApp {
    let root: String
    let workspace: String
    init(_ data: Data) throws {
        let value = object(try JSONSerialization.jsonObject(with: data))
        let root = string(value["root"]), workspace = string(value["workspaceId"])
        guard string(value["format"]) == "melt-launcher/v1", root.hasPrefix("/"),
              workspace.range(of: "^[a-z][a-z0-9_-]{0,63}$", options: .regularExpression) != nil else {
            throw HostError.message("This file is not a supported local Melt app reference.")
        }
        self.root = root; self.workspace = workspace
    }
    var data: Data { get throws {
        try JSONSerialization.data(withJSONObject: ["format": "melt-launcher/v1", "root": root,
                                                    "workspaceId": workspace], options: [.prettyPrinted, .sortedKeys])
    } }
}

final class Bridge {
    private let process = Process()
    private let input = Pipe(), output = Pipe()
    private let queue = DispatchQueue(label: "melt.host.transport")
    private let maximum = 1_048_576

    init(app: LocalApp, configuration: Object) throws {
        let repository = string(configuration["repository"])
        process.executableURL = URL(fileURLWithPath: string(configuration["python"]))
        process.arguments = ["-m", "runtime.desktop", "--root", app.root, "--workspace", app.workspace]
        process.currentDirectoryURL = URL(fileURLWithPath: repository)
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = string(configuration["nodeDirectory"]) + ":" + (environment["PATH"] ?? "/usr/bin:/bin")
        process.environment = environment
        process.standardInput = input; process.standardOutput = output
        // The child keeps project logs in the workspace. Stderr never shares the control pipe.
        process.standardError = FileHandle.standardError
        try process.run()
    }

    private func exact(_ count: Int) throws -> Data {
        var bytes = Data()
        while bytes.count < count {
            let part = output.fileHandleForReading.readData(ofLength: count - bytes.count)
            guard !part.isEmpty else { throw HostError.message("The workspace service closed its channel.") }
            bytes.append(part)
        }
        return bytes
    }

    func request(_ request: Object, completion: @escaping (Result<Object, Error>) -> Void) {
        queue.async { [self] in
            let result = Result<Object, Error> {
                let payload = try JSONSerialization.data(withJSONObject: request)
                guard payload.count <= maximum else { throw HostError.message("The request exceeds the host frame limit.") }
                var length = UInt32(payload.count).bigEndian
                var frame = withUnsafeBytes(of: &length) { Data($0) }
                frame.append(payload)
                try input.fileHandleForWriting.write(contentsOf: frame)
                let header = try exact(4)
                let size = header.reduce(0) { ($0 << 8) | Int($1) }
                guard size <= maximum else { throw HostError.message("The response exceeds the host frame limit.") }
                return object(try JSONSerialization.jsonObject(with: exact(size)))
            }
            DispatchQueue.main.async { completion(result) }
        }
    }

    func close(completion: @escaping () -> Void = {}) {
        queue.async { [self] in
            try? input.fileHandleForWriting.close()
            // EOF asks the service to flush state and stop owned workers.
            process.waitUntilExit()
            DispatchQueue.main.async { completion() }
        }
    }
}
