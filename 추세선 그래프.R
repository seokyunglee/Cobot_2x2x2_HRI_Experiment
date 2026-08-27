# 1. CSV 파일 선택
file_path <- file.choose()

# 2. CSV 읽기
data <- read.csv(
  file_path,
  stringsAsFactors = FALSE
)

# 3. 필요한 열 확인
required_columns <- c(
  "Trial_Num",
  "Elapsed_Time_s",
  "Shoulder_Angle_deg",
  "Target_Shoulder_Angle_deg"
)

missing_columns <- setdiff(required_columns, names(data))

if (length(missing_columns) > 0) {
  stop(
    paste(
      "CSV에 다음 열이 없습니다:",
      paste(missing_columns, collapse = ", ")
    )
  )
}

# 4. Trial 번호 확인
trial_numbers <- sort(unique(data$Trial_Num))
trial_numbers <- trial_numbers[1:min(5, length(trial_numbers))]

# 5. 저장 파일명 만들기
input_name <- tools::file_path_sans_ext(basename(file_path))
output_file <- file.path(
  dirname(file_path),
  paste0(input_name, "_trial_graphs.png")
)

# 6. PNG 장치 열기
png(
  filename = output_file,
  width = 2700,
  height = 3600,
  res = 200
)

# 7. 그래프 배치 설정
par(
  mfrow = c(3, 2),
  mar = c(4, 4.5, 3, 1),
  cex.axis = 0.9,
  cex.lab = 1.0,
  cex.main = 1.1
)

# 8. Trial별 그래프
for (trial in trial_numbers) {

  # Trial 데이터 추출
  trial_data <- data[data$Trial_Num == trial, ]

  # 시간순 정렬
  trial_data <- trial_data[order(trial_data$Elapsed_Time_s), ]

  # 실제 어깨각도 유효 데이터
  valid_actual <- !is.na(trial_data$Elapsed_Time_s) &
                  !is.na(trial_data$Shoulder_Angle_deg)

  actual_data <- trial_data[valid_actual, ]

  # 목표 어깨각도 추출
  target_values <- trial_data$Target_Shoulder_Angle_deg
  target_values <- target_values[!is.na(target_values)]

  if (length(target_values) > 0) {
    target_angle <- median(target_values)
  } else {
    target_angle <- NA
  }

  # 빈 그래프
  plot(
    NA,
    xlim = c(0, 140),
    ylim = c(0, 140),
    xlab = "Elapsed Time (s)",
    ylab = "Shoulder Angle (deg)",
    main = paste("Trial", trial),
    xaxt = "n",
    yaxt = "n"
  )

  # X축 눈금
  axis(
    side = 1,
    at = seq(0, 140, by = 10)
  )

  # Y축 눈금: 5도 간격
  axis(
    side = 2,
    at = seq(0, 140, by = 5),
    las = 1,
    cex.axis = 0.7
  )

  # 격자선
  grid(
    nx = 6,
    ny = 28,
    lty = 3,
    col = "gray85"
  )

  # 실제 어깨각도 선
  if (nrow(actual_data) > 0) {
    lines(
      actual_data$Elapsed_Time_s,
      actual_data$Shoulder_Angle_deg,
      col = "black",
      lwd = 2
    )
  }

  # 목표 어깨각도 빨간 수평선
  if (!is.na(target_angle)) {
    abline(
      h = target_angle,
      col = "red",
      lwd = 2
    )
  }

  # 범례 내용
  if (!is.na(target_angle)) {
    legend_text <- c(
      "Shoulder angle",
      paste0("Target: ", round(target_angle, 1), " deg")
    )
    legend_color <- c("black", "red")
  } else {
    legend_text <- "Shoulder angle"
    legend_color <- "black"
  }

  # 범례: 작게, 박스 없이
  legend(
    "topright",
    legend = legend_text,
    col = legend_color,
    lty = 1,
    lwd = 2,
    cex = 0.6,
    bty = "n"
  )
}

# 9. 남는 칸 비우기
if (length(trial_numbers) < 6) {
  for (i in seq_len(6 - length(trial_numbers))) {
    plot.new()
  }
}

# 10. PNG 저장 완료
dev.off()

# 11. 저장 위치 출력
cat("PNG 저장 완료:\n", output_file, "\n")