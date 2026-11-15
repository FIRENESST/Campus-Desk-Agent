package library;

import java.math.BigDecimal;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class HttpIntegrationTest {
    private static HttpClient client;
    private static String baseUrl;

    public static void main(String[] args) throws Exception {
        client = HttpClient.newHttpClient();
        try (LibraryServer server = new LibraryServer("127.0.0.1", 0, Fixtures.store(), Path.of("web"))) {
            server.start();
            baseUrl = "http://127.0.0.1:" + server.port();
            healthCheckReturnsServiceIdentity();
            exposesExactlyThreeAgentTools();
            administrationConsoleServesManagementPage();
            restAndAgentShareBooksAndLoans();
            malformedAndInvalidRequestsReturnStableErrors();
        }
        System.out.println("HttpIntegrationTest: PASS");
    }

    private static void healthCheckReturnsServiceIdentity() throws Exception {
        HttpResponse<String> response = request("GET", "/healthz", null);
        assertStatus(response, 200);
        Map<String, Object> root = object(response.body());
        if (!root.keySet().equals(Set.of("status", "service"))
                || !"ok".equals(root.get("status")) || !"library".equals(root.get("service"))) {
            throw new AssertionError("健康检查响应不符合契约: " + response.body());
        }
    }

    private static void exposesExactlyThreeAgentTools() throws Exception {
        HttpResponse<String> response = request("GET", "/agent/tools", null);
        assertStatus(response, 200);
        Map<String, Object> root = object(response.body());
        List<?> tools = (List<?>) root.get("tools");
        if (tools == null || tools.size() != 3) {
            throw new AssertionError("Agent 必须恰好提供三个工具: " + response.body());
        }
        Set<String> expectedNames = new LinkedHashSet<>(List.of(
                "library_list_books", "library_borrow_book", "library_return_book"));
        Set<String> actualNames = new LinkedHashSet<>();
        for (Object item : tools) {
            Map<?, ?> tool = (Map<?, ?>) item;
            if (!tool.keySet().equals(Set.of("name", "description", "parameters"))) {
                throw new AssertionError("ToolSpec 字段不符合契约: " + tool);
            }
            actualNames.add((String) tool.get("name"));
            if (!(tool.get("description") instanceof String description) || description.isBlank()) {
                throw new AssertionError("工具描述不能为空");
            }
        }
        if (!actualNames.equals(expectedNames)) {
            throw new AssertionError("工具名称不符合契约: " + actualNames);
        }
        Map<?, ?> listParameters = (Map<?, ?>) ((Map<?, ?>) tools.get(0)).get("parameters");
        Map<?, ?> borrowParameters = (Map<?, ?>) ((Map<?, ?>) tools.get(1)).get("parameters");
        Map<?, ?> returnParameters = (Map<?, ?>) ((Map<?, ?>) tools.get(2)).get("parameters");
        assertSchema(listParameters, Set.of(), Map.of());
        assertSchema(borrowParameters, Set.of("book_id", "borrower_name"), Map.of("duration_days", Map.of(
                "type", "integer", "minimum", BigDecimal.ONE, "maximum", BigDecimal.valueOf(14),
                "default", BigDecimal.valueOf(7))));
        assertSchema(returnParameters, Set.of("loan_id"), Map.of());
    }

    private static void administrationConsoleServesManagementPage() throws Exception {
        HttpResponse<String> response = request("GET", "/admin", null);
        if (response.statusCode() != 200) {
            throw new AssertionError("管理台应返回 200，实际为 " + response.statusCode());
        }
        String contentType = response.headers().firstValue("Content-Type").orElse("");
        if (!contentType.startsWith("text/html")) {
            throw new AssertionError("管理台 Content-Type 不正确: " + contentType);
        }
        for (String text : List.of("图书馆 · 运维管理台", "agent/call")) {
            if (!response.body().contains(text)) {
                throw new AssertionError("管理台页面缺少 " + text);
            }
        }
    }

    private static void restAndAgentShareBooksAndLoans() throws Exception {
        HttpResponse<String> initialResponse = request("GET", "/api/books", null);
        assertStatus(initialResponse, 200);
        List<?> initialBooks = (List<?>) object(initialResponse.body()).get("books");
        assert initialBooks.size() == 11;
        assert initialBooks.stream().map(item -> ((Map<?, ?>) item).get("category_id")).distinct().count() == 8;
        Map<?, ?> pythonBook = assertBookAvailability("b-025", 4);
        assert "computing".equals(pythonBook.get("category_id")) && "计算机与技术".equals(pythonBook.get("category"));

        HttpResponse<String> agentList = request("POST", "/agent/call",
                "{\"name\":\"library_list_books\",\"arguments\":{}}");
        assertStatus(agentList, 200);
        String listContent = (String) object(agentList.body()).get("content");
        if (listContent == null || !initialBooks.equals(object(listContent).get("books"))) {
            throw new AssertionError("Agent 查询 content 应包含图书 JSON 文本: " + agentList.body());
        }

        HttpResponse<String> borrowed = request("POST", "/api/loans",
                "{\"book_id\":\"b-001\",\"borrower_name\":\"前端读者\",\"duration_days\":1}");
        assertStatus(borrowed, 201);
        String restLoanId = (String) ((Map<?, ?>) object(borrowed.body()).get("loan")).get("loan_id");
        assertLoanWindow(assertBookAvailability("b-001", 1), 1);

        HttpResponse<String> agentBorrowed = request("POST", "/agent/call",
                "{\"name\":\"library_borrow_book\",\"arguments\":{\"book_id\":\"b-002\",\"borrower_name\":\"Agent 读者\",\"duration_days\":14}}");
        assertStatus(agentBorrowed, 200);
        String borrowContent = (String) object(agentBorrowed.body()).get("content");
        String agentLoanId = (String) ((Map<?, ?>) object(borrowContent).get("loan")).get("loan_id");
        assertLoanWindow(assertBookAvailability("b-002", 1), 14);
        String anonymous = (String) object(request("POST", "/agent/call",
                "{\"name\":\"library_list_books\",\"arguments\":{}}").body()).get("content");
        if (anonymous.contains("borrower_name") || anonymous.contains(restLoanId) || anonymous.contains(agentLoanId)) {
            throw new AssertionError("馆藏目录不能公开借阅者或借阅编号");
        }

        HttpResponse<String> loansResponse = request("GET", "/api/loans", null);
        assertStatus(loansResponse, 200);
        List<?> loans = (List<?>) object(loansResponse.body()).get("loans");
        if (loans.size() != 2 || !loans.toString().contains(restLoanId) || !loans.toString().contains(agentLoanId)) {
            throw new AssertionError("前端 REST 未看到 Agent 与页面共同写入的借阅: " + loansResponse.body());
        }

        assertError(request("POST", "/agent/call",
                "{\"name\":\"library_borrow_book\",\"arguments\":{\"book_id\":\"b-001\",\"borrower_name\":\"前端读者\"}}"),
                409, "CONFLICT");

        assertStatus(request("POST", "/api/loans", "{\"book_id\":\"b-003\",\"borrower_name\":\"甲\"}"), 201);
        assertError(request("POST", "/api/loans", "{\"book_id\":\"b-003\",\"borrower_name\":\"乙\"}"),
                409, "CAPACITY_FULL");

        assertStatus(request("DELETE", "/api/loans/" + restLoanId, null), 200);
        assert ((List<?>) assertBookAvailability("b-001", 2).get("loan_progress")).isEmpty();
        HttpResponse<String> agentReturned = request("POST", "/agent/call",
                Json.stringify(Map.of("name", "library_return_book", "arguments", Map.of("loan_id", agentLoanId))));
        assertStatus(agentReturned, 200);
        String returnContent = (String) object(agentReturned.body()).get("content");
        if (!agentLoanId.equals(((Map<?, ?>) object(returnContent).get("loan")).get("loan_id"))) {
            throw new AssertionError("Agent 归还结果不包含对应 loan_id");
        }
        assertBookAvailability("b-002", 2);
        assertError(request("DELETE", "/api/loans/" + restLoanId, null), 404, "NOT_FOUND");
    }

    private static void malformedAndInvalidRequestsReturnStableErrors() throws Exception {
        assertError(request("POST", "/api/loans", "{broken"), 400, "INVALID_ARGUMENT");
        assertError(request("POST", "/api/loans", "[]"), 400, "INVALID_ARGUMENT");
        assertError(request("POST", "/api/loans", "{\"book_id\":\"b-004\"}"), 400, "INVALID_ARGUMENT");
        assertError(request("POST", "/api/loans", "{\"book_id\":1,\"borrower_name\":\"丁\"}"), 400, "INVALID_ARGUMENT");
        assertError(request("POST", "/api/loans",
                "{\"book_id\":\"b-004\",\"borrower_name\":\"丁\",\"extra\":true}"), 400, "INVALID_ARGUMENT");
        for (String value : List.of("0", "15", "1.5", "true", "\"2\"")) {
            assertError(request("POST", "/api/loans",
                    "{\"book_id\":\"b-004\",\"borrower_name\":\"丁\",\"duration_days\":" + value + "}"),
                    400, "INVALID_ARGUMENT");
        }
        assertError(request("POST", "/agent/call", "{\"name\":\"unknown\",\"arguments\":{}}"), 404, "NOT_FOUND");
        assertError(request("POST", "/agent/call", "{}"), 400, "INVALID_ARGUMENT");
        assertError(request("POST", "/agent/call", "{\"name\":\"library_list_books\"}"), 400, "INVALID_ARGUMENT");
        assertError(request("POST", "/agent/call", "{\"arguments\":{}}"), 400, "INVALID_ARGUMENT");
        assertError(request("POST", "/agent/call", "{\"name\":1,\"arguments\":{}}"), 400, "INVALID_ARGUMENT");
        assertError(request("POST", "/agent/call", "{\"name\":\"library_list_books\",\"arguments\":null}"), 400, "INVALID_ARGUMENT");
        assertError(request("POST", "/agent/call", "{\"name\":\"library_list_books\",\"arguments\":[]}"), 400, "INVALID_ARGUMENT");
        assertError(request("POST", "/agent/call", "{\"name\":\"library_list_books\",\"arguments\":{},\"extra\":true}"), 400, "INVALID_ARGUMENT");
        assertError(request("POST", "/agent/call", "{\"name\":\"library_list_books\",\"arguments\":{\"extra\":1}}"), 400, "INVALID_ARGUMENT");
        assertError(request("POST", "/agent/call", "{\"name\":\"library_return_book\",\"arguments\":{}}"), 400, "INVALID_ARGUMENT");
        assertError(request("POST", "/agent/call",
                "{\"name\":\"library_return_book\",\"arguments\":{\"loan_id\":\"missing\"}}"), 404, "NOT_FOUND");
        assertError(request("POST", "/agent/call",
                "{\"name\":\"library_borrow_book\",\"arguments\":{\"book_id\":\"b-004\",\"borrower_name\":\"丁\",\"extra\":1}}"),
                400, "INVALID_ARGUMENT");
        assertError(request("GET", "/api/not-found", null), 404, "NOT_FOUND");
    }

    private static void assertSchema(Map<?, ?> schema, Set<String> required, Map<String, ?> optional) {
        if (!"object".equals(schema.get("type")) || !Boolean.FALSE.equals(schema.get("additionalProperties"))) {
            throw new AssertionError("工具参数必须为封闭 object JSON Schema: " + schema);
        }
        Map<?, ?> declared = (Map<?, ?>) schema.get("properties");
        Set<String> properties = new java.util.HashSet<>(required);
        properties.addAll(optional.keySet());
        if (!declared.keySet().equals(properties) || !new java.util.HashSet<>((List<?>) schema.get("required")).equals(required)) {
            throw new AssertionError("工具参数集合不符合契约: " + schema);
        }
        for (String name : required) {
            if (!Map.of("type", "string").equals(declared.get(name))) {
                throw new AssertionError("工具的必填参数必须为 string: " + name);
            }
        }
        for (var entry : optional.entrySet()) {
            if (!entry.getValue().equals(declared.get(entry.getKey()))) {
                throw new AssertionError("借阅天数的工具参数不符合契约: " + declared);
            }
        }
    }

    private static void assertLoanWindow(Map<?, ?> book, int days) {
        List<?> periods = (List<?>) book.get("loan_progress");
        assert periods.size() == 1;
        Map<?, ?> period = (Map<?, ?>) periods.getFirst();
        assert period.keySet().equals(Set.of("borrowed_at", "due_at", "duration_days"));
        assert period.get("duration_days").equals(BigDecimal.valueOf(days));
        var start = java.time.Instant.parse((String) period.get("borrowed_at"));
        var due = java.time.Instant.parse((String) period.get("due_at"));
        assert due.equals(start.plus(days, java.time.temporal.ChronoUnit.DAYS));
    }

    private static Map<?, ?> assertBookAvailability(String bookId, int available) throws Exception {
        HttpResponse<String> response = request("GET", "/api/books", null);
        List<?> books = (List<?>) object(response.body()).get("books");
        for (Object item : books) {
            Map<?, ?> book = (Map<?, ?>) item;
            if (bookId.equals(book.get("id"))) {
                BigDecimal actual = (BigDecimal) book.get("available_copies");
                if (actual.intValueExact() != available || ((BigDecimal) book.get("total_copies")).intValueExact() < available) {
                    throw new AssertionError("图书副本数不符合预期: " + book);
                }
                return book;
            }
        }
        throw new AssertionError("未找到图书 " + bookId);
    }

    private static Map<String, Object> assertError(HttpResponse<String> response, int status, String code) {
        assertStatus(response, status);
        Map<String, Object> root = object(response.body());
        Map<?, ?> error = (Map<?, ?>) root.get("error");
        if (error == null || !code.equals(error.get("code")) || !(error.get("message") instanceof String)
                || !Boolean.FALSE.equals(error.get("retryable"))) {
            throw new AssertionError("错误响应不符合契约: " + response.body());
        }
        return root;
    }

    private static Map<String, Object> object(String json) {
        if (!(Json.parse(json) instanceof Map<?, ?> parsed)) {
            throw new AssertionError("应为 JSON 对象: " + json);
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> object = (Map<String, Object>) parsed;
        return object;
    }

    private static void assertStatus(HttpResponse<String> response, int expected) {
        if (response.statusCode() != expected) {
            throw new AssertionError("HTTP 状态应为 " + expected + "，实际为 " + response.statusCode() + ": " + response.body());
        }
        String contentType = response.headers().firstValue("Content-Type").orElse("");
        if (!contentType.startsWith("application/json")) {
            throw new AssertionError("JSON 接口 Content-Type 不正确: " + contentType);
        }
    }

    private static HttpResponse<String> request(String method, String path, String body) throws Exception {
        HttpRequest.BodyPublisher publisher = body == null
                ? HttpRequest.BodyPublishers.noBody()
                : HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8);
        HttpRequest request = HttpRequest.newBuilder(URI.create(baseUrl + path))
                .header("Content-Type", "application/json; charset=utf-8")
                .method(method, publisher)
                .build();
        return client.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }
}
