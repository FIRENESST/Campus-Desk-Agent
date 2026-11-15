package library;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.math.BigDecimal;
import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class LibraryServer implements AutoCloseable {
    private static final int MAX_BODY_BYTES = 64 * 1024;

    private final HttpServer server;
    private final ExecutorService executor;
    private final LibraryStore store;
    private final Path webRoot;

    public LibraryServer(String host, int port, LibraryStore store, Path webRoot) throws IOException {
        this.server = HttpServer.create(new InetSocketAddress(host, port), 0);
        this.executor = Executors.newVirtualThreadPerTaskExecutor();
        this.store = store;
        this.webRoot = webRoot;
        this.server.createContext("/", this::handle);
        this.server.setExecutor(executor);
    }

    public static void main(String[] args) throws Exception {
        String host = System.getenv().getOrDefault("LIBRARY_HOST", "127.0.0.1");
        int port = Integer.parseInt(System.getenv().getOrDefault("LIBRARY_PORT", "19003"));
        var catalog = LibraryCatalog.load(System.getenv("LIBRARY_CATALOG_FILE"));
        var store = new LibraryStore(Path.of(".data", "loans.json"), java.time.Clock.systemUTC(), catalog);
        LibraryServer server = new LibraryServer(host, port, store, Path.of("web"));
        Runtime.getRuntime().addShutdownHook(new Thread(server::close));
        server.start();
        System.out.println("图书馆服务已启动：http://" + host + ":" + server.port());
        Thread.currentThread().join();
    }

    public void start() {
        server.start();
    }

    public int port() {
        return server.getAddress().getPort();
    }

    @Override
    public void close() {
        server.stop(0);
        executor.close();
    }

    private void handle(HttpExchange exchange) throws IOException {
        try {
            route(exchange);
        } catch (LibraryStore.BusinessException exception) {
            sendError(exchange, businessStatus(exception.code()), exception.code(), exception.getMessage());
        } catch (LibraryRouteException exception) {
            sendError(exchange, businessStatus(exception.code), exception.code, exception.getMessage());
        } catch (IllegalArgumentException exception) {
            sendError(exchange, 400, "INVALID_ARGUMENT", exception.getMessage());
        } catch (Exception exception) {
            sendError(exchange, 500, "INTERNAL_ERROR", "服务暂时无法处理请求");
        } finally {
            exchange.close();
        }
    }

    private void route(HttpExchange exchange) throws IOException {
        String method = exchange.getRequestMethod();
        String path = exchange.getRequestURI().getPath();
        if (path.equals("/healthz") && method.equals("GET")) {
            sendJson(exchange, 200, Map.of("status", "ok", "service", "library"));
            return;
        }
        if (method.equals("GET") && serveStatic(exchange, path)) {
            return;
        }
        if (path.equals("/api/books") && method.equals("GET")) {
            sendJson(exchange, 200, Map.of("books", bookValues(store.catalog())));
            return;
        }
        if (path.equals("/api/loans") && method.equals("GET")) {
            sendJson(exchange, 200, Map.of("loans", loanValues(store.listLoans())));
            return;
        }
        if (path.equals("/api/loans") && method.equals("POST")) {
            LibraryStore.Loan loan = borrow(readObject(exchange));
            sendJson(exchange, 201, Map.of("loan", loanValue(loan)));
            return;
        }
        if (path.startsWith("/api/loans/") && method.equals("DELETE")) {
            String loanId = path.substring("/api/loans/".length());
            if (loanId.isEmpty() || loanId.contains("/")) {
                throw new LibraryRouteException("NOT_FOUND", "未找到请求的资源");
            }
            LibraryStore.Loan loan = store.returnLoan(loanId);
            sendJson(exchange, 200, Map.of("loan", loanValue(loan)));
            return;
        }
        if (path.equals("/agent/tools") && method.equals("GET")) {
            sendJson(exchange, 200, Map.of("tools", toolSpecs()));
            return;
        }
        if (path.equals("/agent/call") && method.equals("POST")) {
            callAgentTool(exchange);
            return;
        }
        if (isKnownApiPath(path)) {
            sendError(exchange, 405, "METHOD_NOT_ALLOWED", "该资源不支持此 HTTP 方法");
            return;
        }
        sendError(exchange, 404, "NOT_FOUND", "未找到请求的资源");
    }

    private boolean serveStatic(HttpExchange exchange, String path) throws IOException {
        String fileName;
        String contentType;
        switch (path) {
            case "/", "/index.html" -> {
                fileName = "index.html";
                contentType = "text/html; charset=utf-8";
            }
            case "/admin" -> {
                fileName = "admin.html";
                contentType = "text/html; charset=utf-8";
            }
            case "/styles.css" -> {
                fileName = "styles.css";
                contentType = "text/css; charset=utf-8";
            }
            case "/app.js" -> {
                fileName = "app.js";
                contentType = "text/javascript; charset=utf-8";
            }
            default -> {
                return false;
            }
        }
        byte[] bytes;
        try {
            bytes = Files.readAllBytes(webRoot.resolve(fileName));
        } catch (NoSuchFileException exception) {
            sendError(exchange, 404, "NOT_FOUND", "未找到页面资源");
            return true;
        }
        exchange.getResponseHeaders().set("Content-Type", contentType);
        exchange.getResponseHeaders().set("Cache-Control", "no-cache");
        exchange.sendResponseHeaders(200, bytes.length);
        try (var output = exchange.getResponseBody()) {
            output.write(bytes);
        }
        return true;
    }

    private void callAgentTool(HttpExchange exchange) throws IOException {
        Map<String, Object> body = readObject(exchange);
        requireExactKeys(body, Set.of("name", "arguments"));
        String name = requiredString(body, "name");
        if (!(body.get("arguments") instanceof Map<?, ?> arguments)) {
            throw new IllegalArgumentException("arguments 必须是对象");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> typedArguments = (Map<String, Object>) arguments;
        Object result;
        switch (name) {
            case "library_list_books" -> {
                requireExactKeys(typedArguments, Set.of());
                result = Map.of("books", bookValues(store.catalog()));
            }
            case "library_borrow_book" -> {
                result = Map.of("loan", loanValue(borrow(typedArguments)));
            }
            case "library_return_book" -> {
                requireExactKeys(typedArguments, Set.of("loan_id"));
                result = Map.of("loan", loanValue(store.returnLoan(requiredString(typedArguments, "loan_id"))));
            }
            case null, default -> throw new LibraryRouteException("NOT_FOUND", "未找到工具: " + name);
        }
        sendJson(exchange, 200, Map.of("content", Json.stringify(result)));
    }

    private LibraryStore.Loan borrow(Map<String, Object> arguments) {
        boolean timed = arguments.containsKey("duration_days");
        requireExactKeys(arguments, timed ? Set.of("book_id", "borrower_name", "duration_days")
                : Set.of("book_id", "borrower_name"));
        int days = LibraryStore.DEFAULT_LOAN_DAYS;
        if (timed) {
            Object value = arguments.get("duration_days");
            if (!(value instanceof BigDecimal number) || number.scale() != 0
                    || number.compareTo(BigDecimal.valueOf(LibraryStore.MIN_LOAN_DAYS)) < 0
                    || number.compareTo(BigDecimal.valueOf(LibraryStore.MAX_LOAN_DAYS)) > 0) {
                throw new IllegalArgumentException("借阅天数必须为 1 到 14 的整数");
            }
            days = number.intValueExact();
        }
        return store.borrow(requiredString(arguments, "book_id"), requiredString(arguments, "borrower_name"), days);
    }

    private static List<Map<String, Object>> toolSpecs() {
        return List.of(
                toolSpec("library_list_books", "查询馆藏图书、分类、可借副本和匿名借期", List.of(), Map.of()),
                toolSpec("library_borrow_book", "为指定读者借阅一本书，可选择 1 到 14 天",
                        List.of("book_id", "borrower_name"),
                        Map.of("duration_days", Map.of("type", "integer", "minimum", LibraryStore.MIN_LOAN_DAYS,
                                "maximum", LibraryStore.MAX_LOAN_DAYS, "default", LibraryStore.DEFAULT_LOAN_DAYS))),
                toolSpec("library_return_book", "按借阅记录 ID 归还图书", List.of("loan_id"), Map.of()));
    }

    private static Map<String, Object> toolSpec(
            String name, String description, List<String> parameters, Map<String, Object> optional) {
        Map<String, Object> properties = new LinkedHashMap<>();
        for (String parameter : parameters) {
            properties.put(parameter, Map.of("type", "string"));
        }
        properties.putAll(optional);
        Map<String, Object> schema = new LinkedHashMap<>();
        schema.put("type", "object");
        schema.put("properties", properties);
        schema.put("required", parameters);
        schema.put("additionalProperties", false);
        Map<String, Object> tool = new LinkedHashMap<>();
        tool.put("name", name);
        tool.put("description", description);
        tool.put("parameters", schema);
        return tool;
    }

    private static List<Map<String, Object>> bookValues(List<LibraryStore.CatalogBook> books) {
        List<Map<String, Object>> values = new ArrayList<>();
        for (LibraryStore.CatalogBook entry : books) {
            LibraryStore.Book book = entry.book();
            Map<String, Object> value = new LinkedHashMap<>();
            value.put("id", book.id());
            value.put("title", book.title());
            value.put("author", book.author());
            value.put("category_id", book.categoryId());
            value.put("category", book.category());
            value.put("total_copies", book.totalCopies());
            value.put("available_copies", book.availableCopies());
            value.put("loan_progress", entry.loans().stream().map(LibraryServer::loanWindowValue).toList());
            values.add(value);
        }
        return values;
    }

    private static List<Map<String, Object>> loanValues(List<LibraryStore.Loan> loans) {
        List<Map<String, Object>> values = new ArrayList<>();
        for (LibraryStore.Loan loan : loans) {
            values.add(loanValue(loan));
        }
        return values;
    }

    private static Map<String, Object> loanValue(LibraryStore.Loan loan) {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("loan_id", loan.id());
        value.put("book_id", loan.bookId());
        value.put("book_title", loan.bookTitle());
        value.put("borrower_name", loan.borrowerName());
        value.putAll(loanWindowValue(loan.window()));
        return value;
    }

    private static Map<String, Object> loanWindowValue(LibraryStore.LoanWindow window) {
        return Map.of("borrowed_at", window.borrowedAt().toString(), "due_at", window.dueAt().toString(),
                "duration_days", window.durationDays());
    }

    private static Map<String, Object> readObject(HttpExchange exchange) throws IOException {
        byte[] bytes;
        try (var input = exchange.getRequestBody()) {
            bytes = input.readNBytes(MAX_BODY_BYTES + 1);
        }
        if (bytes.length > MAX_BODY_BYTES) {
            throw new IllegalArgumentException("请求体不能超过 " + MAX_BODY_BYTES + " 字节");
        }
        String body;
        try {
            body = StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(bytes))
                    .toString();
        } catch (CharacterCodingException exception) {
            throw new IllegalArgumentException("请求体必须是有效 UTF-8", exception);
        }
        if (!(Json.parse(body) instanceof Map<?, ?> object)) {
            throw new IllegalArgumentException("JSON 根节点必须是对象");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> typedObject = (Map<String, Object>) object;
        return typedObject;
    }

    private static void requireExactKeys(Map<String, Object> object, Set<String> expected) {
        if (!object.keySet().equals(expected)) {
            throw new IllegalArgumentException("参数必须且只能包含: " + String.join(", ", expected));
        }
    }

    private static String requiredString(Map<String, Object> object, String key) {
        if (!(object.get(key) instanceof String value)) {
            throw new IllegalArgumentException(key + " 必须是字符串");
        }
        return value;
    }

    private static boolean isKnownApiPath(String path) {
        return path.equals("/healthz") || path.equals("/api/books") || path.equals("/api/loans")
                || path.equals("/agent/tools") || path.equals("/agent/call")
                || path.startsWith("/api/loans/");
    }

    private static int businessStatus(String code) {
        return switch (code) {
            case "NOT_FOUND" -> 404;
            case "CONFLICT", "CAPACITY_FULL" -> 409;
            default -> 400;
        };
    }

    private static void sendError(HttpExchange exchange, int status, String code, String message) throws IOException {
        sendJson(exchange, status, Map.of("error", Map.of(
                "code", code,
                "message", message == null ? "请求无效" : message,
                "retryable", false)));
    }

    private static void sendJson(HttpExchange exchange, int status, Object value) throws IOException {
        byte[] bytes = Json.stringify(value).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.getResponseHeaders().set("Cache-Control", "no-store");
        exchange.sendResponseHeaders(status, bytes.length);
        try (var output = exchange.getResponseBody()) {
            output.write(bytes);
        }
    }

    private static final class LibraryRouteException extends RuntimeException {
        private final String code;

        private LibraryRouteException(String code, String message) {
            super(message);
            this.code = code;
        }
    }
}
