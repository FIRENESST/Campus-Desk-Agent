use axum::{
    body::Body,
    http::{header, Request, StatusCode},
};
use http_body_util::BodyExt;
use schedule::app;
use serde_json::{json, Value};
use tower::ServiceExt;

fn get(uri: &str) -> Request<Body> {
    Request::builder().uri(uri).body(Body::empty()).unwrap()
}

fn post_agent(body: String) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri("/agent/call")
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(body))
        .unwrap()
}

fn call(name: &str, arguments: Value) -> Request<Body> {
    post_agent(json!({"name": name, "arguments": arguments}).to_string())
}

async fn api_json(request: Request<Body>) -> (StatusCode, Value) {
    let lesson = |id: &str, staff: &str, day: &str, start: u8, max_week: u8| {
        json!({
            "course_id": id, "course_name": format!("测试课程 {id}"),
            "teacher": "测试教师", "staff_number": staff, "classroom": "测试教室", "day": day,
            "start_period": start, "end_period": start + 1, "weeks": (1..=max_week).collect::<Vec<_>>()
        })
    };
    let careers = json!({
        "student-test-a": [
            lesson("C101", "t001", "周一", 1, 16), lesson("C102", "t002", "周二", 3, 16),
            lesson("C104", "t004", "周五", 7, 20), lesson("C105", "t005", "周一", 5, 16)
        ],
        "student-test-b": [lesson("C202", "t002", "周一", 3, 16)]
    });
    let response = schedule::app_with_data(&serde_json::to_vec(&careers).unwrap())
        .unwrap()
        .oneshot(request)
        .await
        .unwrap();
    let status = response.status();
    assert_eq!(
        response.headers().get(header::CONTENT_TYPE).unwrap(),
        "application/json"
    );
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap())
}

fn assert_error(status: StatusCode, body: &Value, expected: StatusCode, code: &str) {
    assert_eq!(status, expected);
    assert_eq!(body["error"]["code"], code);
    assert!(body["error"]["message"].as_str().unwrap().len() > 0);
    assert_eq!(body["error"]["retryable"], false);
}

#[tokio::test]
async fn health_reports_schedule_service() {
    let (status, body) = api_json(get("/healthz")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, json!({"status": "ok", "service": "schedule"}));
}

#[tokio::test]
async fn tools_expose_one_closed_schedule_query_schema() {
    let (status, body) = api_json(get("/agent/tools")).await;
    assert_eq!(status, StatusCode::OK);
    let tools = body["tools"].as_array().unwrap();
    assert_eq!(tools.len(), 1);
    assert_eq!(tools[0]["name"], "schedule_query");
    assert!(!tools[0]["description"].as_str().unwrap().is_empty());

    let parameters = &tools[0]["parameters"];
    assert_eq!(parameters["type"], "object");
    assert_eq!(parameters["additionalProperties"], false);
    assert_eq!(parameters["required"], json!(["student_id"]));
    let properties = parameters["properties"].as_object().unwrap();
    assert_eq!(properties.len(), 3);
    assert!(properties.contains_key("student_id"));
    assert!(properties.contains_key("week"));
    assert!(properties.contains_key("day"));
    assert_eq!(parameters["properties"]["week"]["minimum"], 1);
    assert_eq!(parameters["properties"]["week"]["maximum"], 20);
}

#[tokio::test]
async fn native_query_returns_full_courses_for_at_least_two_students() {
    for (student_id, expected_count) in [("student-test-a", 4), ("student-test-b", 1)] {
        let (status, body) = api_json(get(&format!("/api/schedules/{student_id}"))).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["student_id"], student_id);
        let courses = body["courses"].as_array().unwrap();
        assert_eq!(courses.len(), expected_count);
        for course in courses {
            for field in [
                "course_id",
                "course_name",
                "points",
                "teacher",
                "classroom",
                "day",
                "start_period",
                "end_period",
                "weeks",
            ] {
                assert!(course.get(field).is_some(), "missing field {field}");
            }
            assert!(!course["weeks"].as_array().unwrap().is_empty());
            assert!((1..=4).contains(&course["points"].as_u64().unwrap()));
        }
    }
}

#[tokio::test]
async fn native_query_filters_by_week_and_day() {
    let (status, body) = api_json(get("/api/schedules/student-test-a?week=20")).await;
    assert_eq!(status, StatusCode::OK);
    let courses = body["courses"].as_array().unwrap();
    assert_eq!(courses.len(), 1);
    assert_eq!(courses[0]["course_id"], "C104");

    let (status, body) = api_json(get("/api/schedules/student-test-a?week=10&day=周一")).await;
    assert_eq!(status, StatusCode::OK);
    let courses = body["courses"].as_array().unwrap();
    assert_eq!(courses.len(), 2);
    assert_eq!(courses[0]["course_id"], "C101");
    assert_eq!(courses[1]["course_id"], "C105");
}

#[tokio::test]
async fn agent_query_matches_native_query_and_returns_json_text() {
    let arguments = json!({"student_id": "student-test-a", "week": 10, "day": "周一"});
    let (status, body) = api_json(call("schedule_query", arguments)).await;
    assert_eq!(status, StatusCode::OK);
    let content = body["content"].as_str().unwrap();
    let agent_schedule: Value = serde_json::from_str(content).unwrap();
    assert_eq!(
        agent_schedule["periods"][0],
        json!({
            "period": 1, "start_time": "08:00", "end_time": "08:45"
        })
    );

    let (_, native_schedule) =
        api_json(get("/api/schedules/student-test-a?week=10&day=周一")).await;
    assert_eq!(agent_schedule, native_schedule);
}

#[tokio::test]
async fn teacher_schedule_returns_only_that_staff_members_classes_for_the_week() {
    let (status, body) = api_json(get("/api/teaching/t002?week=3")).await;
    assert_eq!(status, StatusCode::OK);
    let courses = body["courses"].as_array().unwrap();
    assert_eq!(courses.len(), 2);
    assert_eq!(courses[0]["course_id"], "C202");
    assert_eq!(courses[1]["course_id"], "C102");
    assert!(courses
        .iter()
        .all(|course| course.get("staff_number").is_none()));
    assert!(body.get("student_id").is_none());
    assert_eq!(body["periods"][3]["end_time"], "11:40");
    let (status, semester) = api_json(get("/api/teaching/t002")).await;
    assert_eq!(status, StatusCode::OK);
    let term_courses = semester["courses"].as_array().unwrap();
    for course in courses {
        assert!(term_courses.contains(course));
    }
    let (_, student) = api_json(get("/api/schedules/student-test-a")).await;
    let english = student["courses"]
        .as_array()
        .unwrap()
        .iter()
        .find(|course| course["course_id"] == "C102")
        .unwrap();
    assert_eq!(courses[1]["points"], english["points"]);

    for uri in ["/api/teaching/t002?week=20", "/api/teaching/t999?week=3"] {
        let (status, body) = api_json(get(uri)).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["courses"], json!([]));
    }
}

#[tokio::test]
async fn agent_rejects_malformed_json() {
    let (status, body) = api_json(post_agent("{bad".to_string())).await;
    assert_error(status, &body, StatusCode::BAD_REQUEST, "INVALID_ARGUMENT");
}

#[tokio::test]
async fn agent_strictly_rejects_missing_or_extra_request_fields() {
    for payload in [
        json!({"arguments": {"student_id": "student-test-a"}}),
        json!({"name": "schedule_query"}),
        json!({"name": "schedule_query", "arguments": {"student_id": "student-test-a"}, "extra": true}),
        json!({"name": 1, "arguments": {"student_id": "student-test-a"}}),
        json!([]),
    ] {
        let (status, body) = api_json(post_agent(payload.to_string())).await;
        assert_error(status, &body, StatusCode::BAD_REQUEST, "INVALID_ARGUMENT");
    }
}

#[tokio::test]
async fn agent_rejects_non_object_arguments_and_invalid_student_id() {
    for arguments in [json!(null), json!([]), json!("value"), json!(1)] {
        let (status, body) = api_json(call("schedule_query", arguments)).await;
        assert_error(status, &body, StatusCode::BAD_REQUEST, "INVALID_ARGUMENT");
    }
    for arguments in [
        json!({}),
        json!({"student_id": ""}),
        json!({"student_id": "   "}),
        json!({"student_id": 123}),
        json!({"student_id": "student-test-a", "unknown": true}),
    ] {
        let (status, body) = api_json(call("schedule_query", arguments)).await;
        assert_error(status, &body, StatusCode::BAD_REQUEST, "INVALID_ARGUMENT");
    }
}

#[tokio::test]
async fn agent_rejects_out_of_range_or_non_integer_weeks() {
    for week in [
        json!(0),
        json!(21),
        json!(-1),
        json!(1.5),
        json!("1"),
        json!(null),
    ] {
        let (status, body) = api_json(call(
            "schedule_query",
            json!({"student_id": "student-test-a", "week": week}),
        ))
        .await;
        assert_error(status, &body, StatusCode::BAD_REQUEST, "INVALID_ARGUMENT");
    }
}

#[tokio::test]
async fn agent_rejects_invalid_days() {
    for day in [
        json!("星期一"),
        json!(""),
        json!("monday"),
        json!(1),
        json!(null),
    ] {
        let (status, body) = api_json(call(
            "schedule_query",
            json!({"student_id": "student-test-a", "day": day}),
        ))
        .await;
        assert_error(status, &body, StatusCode::BAD_REQUEST, "INVALID_ARGUMENT");
    }
}

#[tokio::test]
async fn unknown_students_and_tools_are_not_found() {
    let (status, body) = api_json(call(
        "schedule_query",
        json!({"student_id": "does-not-exist"}),
    ))
    .await;
    assert_error(status, &body, StatusCode::NOT_FOUND, "NOT_FOUND");

    let (status, body) = api_json(call(
        "unknown_tool",
        json!({"student_id": "student-test-a"}),
    ))
    .await;
    assert_error(status, &body, StatusCode::NOT_FOUND, "NOT_FOUND");
}

#[tokio::test]
async fn agent_rejects_bodies_larger_than_64_kib() {
    let valid = r#"{"name":"schedule_query","arguments":{"student_id":"student-test-a"}}"#;
    let oversized = format!("{valid}{}", " ".repeat(70 * 1024));
    let (status, body) = api_json(post_agent(oversized)).await;
    assert_error(status, &body, StatusCode::BAD_REQUEST, "INVALID_ARGUMENT");
}

#[tokio::test]
async fn native_query_rejects_invalid_filters_and_unknown_students() {
    for uri in [
        "/api/schedules/student-test-a?week=0",
        "/api/schedules/student-test-a?week=21",
        "/api/schedules/student-test-a?week=nope",
        "/api/schedules/student-test-a?day=星期一",
        "/api/schedules/student-test-a?unknown=true",
        "/api/schedules/%20%20%20",
    ] {
        let (status, body) = api_json(get(uri)).await;
        assert_error(status, &body, StatusCode::BAD_REQUEST, "INVALID_ARGUMENT");
    }
    let (status, body) = api_json(get("/api/schedules/does-not-exist")).await;
    assert_error(status, &body, StatusCode::NOT_FOUND, "NOT_FOUND");
}

#[tokio::test]
async fn admin_console_serves_management_page() {
    let response = app().oneshot(get("/admin")).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert!(response.headers()[header::CONTENT_TYPE]
        .to_str()
        .unwrap()
        .starts_with("text/html"));
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    let html = std::str::from_utf8(&bytes).unwrap();
    for text in ["课表 · 运维管理台", "agent/call"] {
        assert!(html.contains(text), "admin page missing {text}");
    }
}

#[tokio::test]
async fn static_frontend_is_chinese_and_calls_the_schedule_api() {
    let response = app().oneshot(get("/")).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert!(response.headers()[header::CONTENT_TYPE]
        .to_str()
        .unwrap()
        .starts_with("text/html"));
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    let html = std::str::from_utf8(&bytes).unwrap();
    for text in ["课表查询", "学号", "周次", "星期"] {
        assert!(html.contains(text), "frontend missing {text}");
    }
    assert!(html.contains("fetch("));
    assert!(html.contains("/api/schedules/"));
    assert!(html.contains("week"));
    assert!(html.contains("day"));
}

#[tokio::test]
async fn production_default_contains_no_courses() {
    let response = app()
        .oneshot(get("/api/schedules/student-test-a"))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}
