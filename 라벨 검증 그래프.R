# Label-review plot for shoulder-angle time series
#
# Select one or more *_analysis_samples.csv files. The paired
# *_analysis_segments.csv file is found automatically for each selection.
# One label-review PNG is saved next to each selected sample file.

X_MAX_S <- 180
Y_MIN_DEG <- 0
Y_MAX_DEG <- 140

LABEL_COLORS <- c(
  "Work" = "#9ECAE1",         # light blue
  "Rest" = "#A1D99B",         # light green
  "Transition" = "#FDD0A2",   # light orange
  "Speech_Wait" = "#DADAEB"    # light purple/grey
)

# 1. Select one or more frame-level analysis files.
sample_paths <- choose.files(
  caption = "Select analysis_samples CSV files for label review",
  multi = TRUE,
  filters = matrix(c("Analysis samples CSV", "*_analysis_samples.csv", "CSV files", "*.csv"), ncol = 2, byrow = TRUE)
)

if (length(sample_paths) == 0) {
  stop("No files were selected.")
}

for (sample_path in sample_paths) {
  if (!grepl("_analysis_samples\\.csv$", sample_path, ignore.case = TRUE)) {
    warning(paste("Skipped: file must end in _analysis_samples.csv:", sample_path))
    next
  }

  segment_path <- sub(
    "_analysis_samples\\.csv$",
    "_analysis_segments.csv",
    sample_path,
    ignore.case = TRUE
  )

  if (!file.exists(segment_path)) {
    warning(paste("Skipped: paired segment file was not found:", segment_path))
    next
  }

  # 2. Read paired files.
  samples <- read.csv(sample_path, stringsAsFactors = FALSE)
  segments <- read.csv(segment_path, stringsAsFactors = FALSE)

required_sample_columns <- c(
  "Trial_Num",
  "Elapsed_Time_s",
  "Shoulder_Angle_deg",
  "Target_Shoulder_Angle_deg"
)
required_segment_columns <- c(
  "Trial_Num",
  "Label",
  "Start_Time_s",
  "End_Time_s"
)

  missing_samples <- setdiff(required_sample_columns, names(samples))
  missing_segments <- setdiff(required_segment_columns, names(segments))

  if (length(missing_samples) > 0) {
    warning(paste("Skipped: sample file is missing:", paste(missing_samples, collapse = ", ")))
    next
  }
  if (length(missing_segments) > 0) {
    warning(paste("Skipped: segment file is missing:", paste(missing_segments, collapse = ", ")))
    next
  }

trial_numbers <- sort(unique(samples$Trial_Num))
trial_numbers <- trial_numbers[1:min(5, length(trial_numbers))]

input_name <- tools::file_path_sans_ext(basename(sample_path))
input_name <- sub("_analysis_samples$", "", input_name)
output_file <- file.path(
  dirname(sample_path),
  paste0(input_name, "_label_review.png")
)

# 3. Create a tall image: this is a review figure, not a compact summary.
png(
  filename = output_file,
  width = 3000,
  height = 5000,
  res = 200
)

par(
  mfrow = c(5, 1),
  mar = c(3.5, 4.5, 3, 1.2),
  cex.axis = 0.9,
  cex.lab = 1.0,
  cex.main = 1.1
)

for (trial in trial_numbers) {
  trial_samples <- samples[samples$Trial_Num == trial, ]
  trial_samples <- trial_samples[order(trial_samples$Elapsed_Time_s), ]

  trial_segments <- segments[segments$Trial_Num == trial, ]
  trial_segments <- trial_segments[order(trial_segments$Start_Time_s), ]

  valid_actual <- !is.na(trial_samples$Elapsed_Time_s) &
    !is.na(trial_samples$Shoulder_Angle_deg)
  actual_data <- trial_samples[valid_actual, ]

  target_values <- trial_samples$Target_Shoulder_Angle_deg
  target_values <- target_values[!is.na(target_values)]
  target_angle <- if (length(target_values) > 0) median(target_values) else NA

  # Blank plotting region first, so state bands can be drawn behind the data.
  plot(
    NA,
    xlim = c(0, X_MAX_S),
    ylim = c(Y_MIN_DEG, Y_MAX_DEG),
    xlab = "Elapsed Time (s)",
    ylab = "Shoulder Angle (deg)",
    main = paste("Trial", trial),
    xaxt = "n",
    yaxt = "n"
  )

  # Background state bands. The colour is intentionally transparent so the
  # original shoulder-angle line remains the primary visual evidence.
  if (nrow(trial_segments) > 0) {
    for (row_index in seq_len(nrow(trial_segments))) {
      segment <- trial_segments[row_index, ]
      label <- as.character(segment$Label)
      fill_colour <- LABEL_COLORS[label]
      if (is.na(fill_colour)) {
        fill_colour <- "#D9D9D9"
      }

      band_left <- max(0, min(X_MAX_S, segment$Start_Time_s))
      band_right <- max(0, min(X_MAX_S, segment$End_Time_s))
      if (band_right > band_left) {
        rect(
          xleft = band_left,
          ybottom = Y_MIN_DEG,
          xright = band_right,
          ytop = Y_MAX_DEG,
          col = adjustcolor(fill_colour, alpha.f = 0.35),
          border = NA
        )
      }

      # Thin boundaries make it easy to inspect the precise split location.
      if (row_index > 1 && segment$Start_Time_s >= 0 && segment$Start_Time_s <= X_MAX_S) {
        abline(
          v = segment$Start_Time_s,
          col = adjustcolor("gray30", alpha.f = 0.45),
          lty = 3,
          lwd = 0.8
        )
      }
    }
  }

  grid(
    nx = 12,
    ny = 14,
    lty = 3,
    col = "gray80"
  )

  axis(side = 1, at = seq(0, X_MAX_S, by = 10))
  axis(side = 2, at = seq(Y_MIN_DEG, Y_MAX_DEG, by = 10), las = 1)

  if (nrow(actual_data) > 0) {
    lines(
      actual_data$Elapsed_Time_s,
      actual_data$Shoulder_Angle_deg,
      col = "black",
      lwd = 2
    )
  }

  if (!is.na(target_angle)) {
    abline(h = target_angle, col = "red", lwd = 2)
  }

  # Keep the line legend separate from the state-band legend.
  line_legend <- if (!is.na(target_angle)) {
    c("Shoulder angle", paste0("Target: ", round(target_angle, 1), " deg"))
  } else {
    "Shoulder angle"
  }
  line_colours <- if (!is.na(target_angle)) c("black", "red") else "black"

  legend(
    "topright",
    legend = line_legend,
    col = line_colours,
    lty = 1,
    lwd = 2,
    cex = 0.75,
    bty = "n"
  )

  present_labels <- intersect(names(LABEL_COLORS), unique(as.character(trial_segments$Label)))
  if (length(present_labels) > 0) {
    legend(
      "bottomleft",
      legend = present_labels,
      fill = adjustcolor(LABEL_COLORS[present_labels], alpha.f = 0.35),
      border = NA,
      cex = 0.75,
      bty = "n"
    )
  }
}

# Fill unused panels if fewer than five trials exist.
if (length(trial_numbers) < 5) {
  for (index in seq_len(5 - length(trial_numbers))) {
    plot.new()
  }
}

  dev.off()
  cat("Created label-review plot:\n", output_file, "\n")
}
