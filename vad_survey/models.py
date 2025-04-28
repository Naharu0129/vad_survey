from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

class Word(models.Model):
    text = models.CharField(max_length=100)
    POS = models.CharField(max_length=100)
    valence_score = models.FloatField(null=True, blank=True)  # VAD 점수 필드 추가
    arousal_score = models.FloatField(null=True, blank=True)
    dominance_score = models.FloatField(null=True, blank=True)
    total_ratings = models.IntegerField(default=0)  # 평가 횟수 트래킹
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.text


class Rating(models.Model):
    DIMENSIONS = (
        ('V', 'Valence'),
        ('A', 'Arousal'),
        ('D', 'Dominance'),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    word_tuple = models.ForeignKey('WordTuple', on_delete=models.CASCADE)
    dimension = models.CharField(max_length=1, choices=DIMENSIONS)
    best_word = models.ForeignKey('Word', on_delete=models.CASCADE, related_name='best_ratings')
    worst_word = models.ForeignKey('Word', on_delete=models.CASCADE, related_name='worst_ratings')

    response_time = models.IntegerField(
        null=True,
        help_text="Response time in milliseconds"
    )
    start_time = models.DateTimeField(
        null=True,
        help_text="When the user started this rating"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'word_tuple', 'dimension')
        constraints = [
            models.CheckConstraint(
                check=models.Q(dimension__in=['V', 'A', 'D']),
                name='valid_dimension'
            ),
            models.CheckConstraint(
                check=~models.Q(best_word_id=models.F('worst_word_id')),
                name='different_best_worst_words'
            )
        ]
        indexes = [
            models.Index(fields=['user', 'word_tuple', 'dimension']),
            models.Index(fields=['created_at']),
        ]

    def clean(self):
        """
        Rating 객체의 유효성을 검증합니다.
        """
        # 기본 검증
        if not self.user or not self.word_tuple or not self.best_word or not self.worst_word:
            raise ValidationError("All required fields must be provided")

        # 같은 단어가 best와 worst로 선택되지 않도록 검증
        if self.best_word_id == self.worst_word_id:
            raise ValidationError({
                "worst_word": "Best word and worst word cannot be the same"
            })

        # best_word와 worst_word가 해당 word_tuple에 속하는지 검증
        tuple_words = set(self.word_tuple.words.all())
        if self.best_word not in tuple_words or self.worst_word not in tuple_words:
            raise ValidationError({
                "best_word": "Selected words must belong to the word tuple",
                "worst_word": "Selected words must belong to the word tuple"
            })

        # 차원이 word_tuple의 차원과 일치하는지 검증
        if self.word_tuple.dimension != self.dimension:
            raise ValidationError({
                "dimension": f"This tuple is for {self.word_tuple.get_dimension_display()} ratings"
            })

        # 응답 시간이 너무 짧거나 긴 경우 검증 (예: 0.5초 미만 또는 5분 초과)
        if self.start_time and self.response_time:
            if self.response_time < 500:  # 500ms = 0.5초
                raise ValidationError({
                    "response_time": "Response time is too short"
                })
            if self.response_time > 300000:  # 300000ms = 5분
                raise ValidationError({
                    "response_time": "Response time is too long"
                })

    def calculate_response_time(self):
        """
        시작 시간부터 현재까지의 응답 시간을 계산합니다 (milliseconds).
        """
        if self.start_time:

            time_diff = timezone.now() - self.start_time
            self.response_time = int(time_diff.total_seconds() * 1000)

    def update_user_metrics(self):
        """
        사용자의 평가 지표를 업데이트합니다.
        """
        profile = self.user.userprofile
        profile.total_ratings += 1
        profile.last_rating_at = self.created_at

        # Gold question인 경우 정확도 업데이트
        if self.word_tuple.is_gold:
            is_correct = (
                    self.best_word_id == self.word_tuple.gold_best_word_id and
                    self.worst_word_id == self.word_tuple.gold_worst_word_id
            )
            profile.update_gold_accuracy(is_correct)

        profile.save()

    def update_word_metrics(self):
        """
        평가된 단어들의 지표를 업데이트합니다.
        """
        self.best_word.total_ratings += 1
        self.worst_word.total_ratings += 1
        self.best_word.save()
        self.worst_word.save()

    def save(self, *args, **kwargs):
        # 응답 시간 계산
        self.calculate_response_time()

        # 유효성 검증
        self.clean()

        # 저장
        super().save(*args, **kwargs)

        # 관련 지표 업데이트
        self.update_user_metrics()
        self.update_word_metrics()

        # word_tuple이 완료되었는지 확인
        self.word_tuple.check_completion()

    def __str__(self):
        return (f"{self.user.username}'s {self.get_dimension_display()} "
                f"rating for Tuple {self.word_tuple.id}")


class WordTuple(models.Model):
    words = models.ManyToManyField(Word)
    is_gold = models.BooleanField(default=False)
    gold_best_word = models.ForeignKey(
        Word,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='gold_best_tuples'
    )
    gold_worst_word = models.ForeignKey(
        Word,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='gold_worst_tuples'
    )
    dimension = models.CharField(
        max_length=1,
        choices=Rating.DIMENSIONS,
        null=True,  # 기존 데이터와의 호환성을 위해
        blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        word_list = [w.text for w in self.words.all()]
        return f"Tuple {self.id}: " + ", ".join(word_list)

    def get_missing_dimensions(self, user):
        rated_dimensions = set(
            self.rating_set.filter(user=user).values_list('dimension', flat=True)
        )
        all_dimensions = set(code for code, _ in Rating.DIMENSIONS)
        return all_dimensions - rated_dimensions

    def is_fully_rated_by_user(self, user):
        return not bool(self.get_missing_dimensions(user))

    def check_completion(self):
        """
        이 튜플에 대한 모든 평가가 완료되었는지 확인합니다.
        """
        # 이 튜플과 연결된 모든 UserWordTuple 확인
        user_tuples = UserWordTuple.objects.filter(word_tuple=self)

        for user_tuple in user_tuples:
            # 해당 사용자가 이 튜플의 차원에 대해 평가했는지 확인
            if self.dimension:  # dimension이 설정된 경우만 확인
                is_rated = Rating.objects.filter(
                    user=user_tuple.user,
                    word_tuple=self,
                    dimension=self.dimension
                ).exists()

                # 평가되었으면 completed 플래그 설정
                if is_rated and not user_tuple.completed:
                    user_tuple.completed = True
                    user_tuple.save(update_fields=['completed'])


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    age = models.IntegerField()
    gender = models.CharField(max_length=20)
    personality_type = models.CharField(max_length=50)
    gold_accuracy = models.FloatField(default=0.0)  # Gold question 정확도
    total_ratings = models.IntegerField(default=0)  # 총 평가 수
    is_active = models.BooleanField(default=True)  # 80% 정확도 기준
    last_rating_at = models.DateTimeField(null=True, blank=True)

    def update_gold_accuracy(self, is_correct: bool):
        """새로운 gold 평가 결과를 누적하여 정확도 계산"""
        self.total_ratings += 1

        # 현재 정확도는 백분율이므로 백분율 → 정수 개수로 환산
        correct_so_far = round(self.gold_accuracy * (self.total_ratings - 1) / 100)

        if is_correct:
            correct_so_far += 1

        # 다시 백분율로 계산
        self.gold_accuracy = (correct_so_far / self.total_ratings) * 100

        # 정확도가 80% 미만이면 비활성화
        self.is_active = self.gold_accuracy >= 80
        self.save()

class UserWordTuple(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    word_tuple = models.ForeignKey(WordTuple, on_delete=models.CASCADE)
    assigned_at = models.DateTimeField(auto_now_add=True)
    completed = models.BooleanField(default=False)

    class Meta:
        unique_together = ('user', 'word_tuple')

    def is_fully_rated(self):
        rated_dimensions = Rating.objects.filter(
            user=self.user,
            word_tuple=self.word_tuple
        ).values_list('dimension', flat=True)
        return len(set(rated_dimensions)) == len(Rating.DIMENSIONS)

    def __str__(self):
        word_list = ", ".join([w.text for w in self.word_tuple.words.all()])
        return f"{self.user.username} - [{word_list}]"
