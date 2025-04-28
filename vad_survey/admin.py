# vad_survey/admin.py
import csv
from django import forms
from django.contrib import admin
from django.contrib.admin import SimpleListFilter
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db.models import Q
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.template.response import TemplateResponse
from django.urls import reverse, path
from django.utils.html import format_html
from django.utils.timezone import timedelta
from import_export import resources
from import_export.admin import ImportExportModelAdmin

from .models import UserWordTuple
from .models import Word, WordTuple, Rating, UserProfile


# Word 모델용 리소스 클래스
class WordResource(resources.ModelResource):
    class Meta:
        model = Word
        import_id_fields = ['text']
        fields = ('text', 'POS','valence_score', 'arousal_score', 'dominance_score')


# Word 모델 관리자 페이지
class WordAdmin(ImportExportModelAdmin):
    resource_class = WordResource
    list_display = ('text', 'POS','valence_score', 'arousal_score', 'dominance_score', 'total_ratings')
    list_filter = ('POS', )
    search_fields = ('text',)
    actions = ['generate_bws_tuples']
    change_list_template = 'admin/word_changelist.html'
    list_max_show_all = 100000

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('generate_bws_tuples/',
                 self.admin_site.admin_view(self.generate_bws_tuples_view),
                 name='vad_survey_word_generate_bws_tuples'),
        ]
        return custom_urls + urls

    def generate_bws_tuples_view(self, request):
        # GET 요청 시 설정 폼 표시
        if request.method == 'GET':
            context = {
                'title': 'Generate BWS Tuples',
                'app_label': 'vad_survey',
                'opts': self.model._meta,
                'has_view_permission': self.has_view_permission(request),
            }
            return TemplateResponse(request, 'admin/generate_bws_tuples.html', context)

        # POST 요청 시 튜플 생성 실행
        elif request.method == 'POST':
            items_per_tuple = int(request.POST.get('items_per_tuple', 4))
            scaling_factor = float(request.POST.get('scaling_factor', 2.0))
            iterations = int(request.POST.get('iterations', 100))
            dimension = request.POST.get('dimension', 'V')

            # 관리 명령어 호출
            call_command(
                'generate_bws_tuples',
                items_per_tuple=items_per_tuple,
                scaling_factor=scaling_factor,
                iterations=iterations,
                dimension=dimension
            )

            self.message_user(request, f"BWS 튜플 생성이 완료되었습니다. (차원: {dimension})")
            return HttpResponseRedirect("../")

    @admin.action(description="선택한 단어로 BWS 튜플 생성")
    def generate_bws_tuples(self, request, queryset):
        """선택된 단어들로 BWS 튜플 생성 (액션)"""
        # 모든 단어 대신 선택된 단어만 사용
        if not queryset.exists():
            self.message_user(request, "튜플 생성을 위해 단어를 선택해주세요")
            return

        # 간단한 버전 - 기본 설정으로 튜플 생성
        words = list(queryset.values_list('text', flat=True))
        word_ids = list(queryset.values_list('id', flat=True))
        # 기본 차원에 대해 튜플 생성
        dimension = 'V'

        from django.core.management import call_command
        call_command('generate_bws_tuples', dimension=dimension, word_ids=",".join(map(str, word_ids)))

        self.message_user(
            request,
            f"{len(words)}개 단어로 BWS 튜플 생성이 완료되었습니다. (차원: {dimension})"
        )

    generate_bws_tuples.short_description = "선택한 단어로 BWS 튜플 생성"


# WordTuple 모델 관리자 페이지
class WordTupleAdmin(admin.ModelAdmin):
    list_display = ('id', 'dimension', 'display_words', 'is_gold', 'created_at')
    list_filter = ('dimension', 'is_gold', 'created_at')
    search_fields = ('words__text',)
    list_max_show_all = 100000

    def display_words(self, obj):
        return ", ".join([word.text for word in obj.words.all()])

    display_words.short_description = "단어"


# 사용자-튜플 할당 필터
class CompletionStatusFilter(SimpleListFilter):
    title = '완료 상태'
    parameter_name = 'completion_status'

    def lookups(self, request, model_admin):
        return (
            ('completed', '완료됨'),
            ('in_progress', '진행 중'),
            ('not_started', '시작 안 함'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'completed':
            return queryset.filter(completed=True)
        if self.value() == 'in_progress':
            return queryset.filter(completed=False).exclude(rating__isnull=True).distinct()
        if self.value() == 'not_started':
            return queryset.filter(completed=False, rating__isnull=True)


# 튜플 할당 폼
class TupleAssignmentForm(forms.Form):
    users = forms.ModelMultipleChoiceField(
        queryset=User.objects.filter(is_staff=False),
        label='작업자',
        widget=forms.CheckboxSelectMultiple
    )
    dimension = forms.ChoiceField(
        choices=[('V', 'Valence (감정가)'), ('A', 'Arousal (각성도)'), ('D', 'Dominance (지배성)')],
        label='차원',
        widget=forms.RadioSelect
    )
    tuples_per_user = forms.IntegerField(
        min_value=1,
        max_value=100,
        initial=20,
        label='작업자당 튜플 수'
    )
    assign_method = forms.ChoiceField(
        choices=[
            ('random', '무작위 할당'),
            ('least_rated', '평가 수가 적은 튜플 우선 할당'),
        ],
        label='할당 방식',
        widget=forms.RadioSelect
    )
    include_gold = forms.BooleanField(
        required=False,
        initial=True,
        label='검증용(Gold) 튜플 포함'
    )
    gold_percentage = forms.IntegerField(
        min_value=5,
        max_value=30,
        initial=10,
        label='검증용 튜플 비율 (%)',
        required=False
    )


# 사용자-튜플 할당 관리자 페이지
class UserWordTupleAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'user_link', 'tuple_link', 'tuple_words_display',
        'dimension_display', 'assigned_at', 'completion_status', 'completion_time'
    )
    change_list_template = 'admin/user_wordtuple_changelist.html'
    list_filter = ('word_tuple__dimension', CompletionStatusFilter, 'assigned_at')
    search_fields = ('user__username', 'word_tuple__id')
    date_hierarchy = 'assigned_at'
    list_select_related = ('user', 'word_tuple')
    actions = ['mark_as_completed', 'mark_as_not_completed', 'reassign_tuples']
    list_per_page = 50

    def tuple_words_display(self, obj):
        return ", ".join([word.text for word in obj.word_tuple.words.all()])

    tuple_words_display.short_description = '단어 목록'

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('assign-tuples/',
                 self.admin_site.admin_view(self.assign_tuples_view),
                 name='assign-tuples'),
            path('dashboard/',
                 assignment_dashboard,
                 name='assignment-dashboard'),
        ]
        return custom_urls + urls

    def user_link(self, obj):
        url = reverse('admin:auth_user_change', args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    user_link.short_description = '작업자'
    user_link.admin_order_field = 'user__username'

    def tuple_link(self, obj):
        url = reverse('admin:vad_survey_wordtuple_change', args=[obj.word_tuple.id])
        return format_html('<a href="{}">{}</a>', url, f'튜플 #{obj.word_tuple.id}')

    tuple_link.short_description = '단어 튜플'
    tuple_link.admin_order_field = 'word_tuple__id'

    def dimension_display(self, obj):
        dimensions = {
            'V': '감정가 (Valence)',
            'A': '각성도 (Arousal)',
            'D': '지배성 (Dominance)'
        }
        return dimensions.get(obj.word_tuple.dimension, obj.word_tuple.dimension)

    dimension_display.short_description = '차원'
    dimension_display.admin_order_field = 'word_tuple__dimension'

    def completion_status(self, obj):
        if obj.completed:
            return format_html('<span style="color:green">완료</span>')

        # 완료되지 않았지만 일부 평가가 있는지 확인
        rating_count = obj.word_tuple.rating_set.filter(user=obj.user).count()
        if rating_count > 0:
            return format_html('<span style="color:orange">진행 중 ({}/3)</span>', rating_count)
        else:
            return format_html('<span style="color:red">시작 안 함</span>')

    completion_status.short_description = '상태'

    def completion_time(self, obj):
        if not obj.completed:
            return '-'
        try:
            last_rating = obj.word_tuple.rating_set.filter(user=obj.user).latest('created_at')
            time_diff = last_rating.created_at - obj.assigned_at
            hours = time_diff.total_seconds() // 3600
            minutes = (time_diff.total_seconds() % 3600) // 60
            return f"{int(hours)}시간 {int(minutes)}분"
        except:
            return '-'

    completion_time.short_description = '소요 시간'

    def mark_as_completed(self, request, queryset):
        queryset.update(completed=True)
        self.message_user(request, f"{queryset.count()}개 할당이 완료로 표시되었습니다.")

    mark_as_completed.short_description = "선택한 할당을 완료로 표시"

    def mark_as_not_completed(self, request, queryset):
        queryset.update(completed=False)
        self.message_user(request, f"{queryset.count()}개 할당이 미완료로 표시되었습니다.")

    mark_as_not_completed.short_description = "선택한 할당을 미완료로 표시"

    def reassign_tuples(self, request, queryset):
        for assignment in queryset:
            # 기존 평가 데이터 삭제
            assignment.word_tuple.rating_set.filter(user=assignment.user).delete()
            # 할당 초기화
            assignment.completed = False
            assignment.save()
        self.message_user(request, f"{queryset.count()}개 할당이 재설정되었습니다.")

    reassign_tuples.short_description = "선택한 할당을 재설정 (평가 데이터 삭제)"

    def assign_tuples_view(self, request):
        """작업자에게 튜플 할당 뷰"""
        if request.method == 'POST':
            form = TupleAssignmentForm(request.POST)
            if form.is_valid():
                users = form.cleaned_data['users']
                dimension = form.cleaned_data['dimension']
                tuples_per_user = form.cleaned_data['tuples_per_user']
                assign_method = form.cleaned_data['assign_method']
                include_gold = form.cleaned_data['include_gold']
                gold_percentage = form.cleaned_data.get('gold_percentage', 10)

                from django.db import transaction
                from vad_survey.models import WordTuple
                import random

                with transaction.atomic():
                    # 해당 차원의 사용 가능한 튜플 가져오기
                    tuples_query = WordTuple.objects.filter(dimension=dimension)

                    if include_gold:
                        # 골드 튜플과 일반 튜플 분리
                        gold_tuples = list(tuples_query.filter(is_gold=True))
                        regular_tuples = list(tuples_query.filter(is_gold=False))

                        # 골드 튜플이 충분한지 확인
                        gold_count = int(tuples_per_user * gold_percentage / 100)
                        gold_count = min(gold_count, len(gold_tuples))
                    else:
                        gold_tuples = []
                        regular_tuples = list(tuples_query)
                        gold_count = 0

                    # 일반 튜플 수 계산
                    regular_count = tuples_per_user - gold_count

                    # 각 작업자에게 튜플 할당
                    total_assigned = 0
                    for user in users:
                        # 해당 사용자에게 이미 할당된 튜플 제외
                        user_existing_tuples = set(UserWordTuple.objects.filter(
                            user=user
                        ).values_list('word_tuple__id', flat=True))

                        available_regular = [t for t in regular_tuples if t.id not in user_existing_tuples]
                        available_gold = [t for t in gold_tuples if t.id not in user_existing_tuples]

                        if len(available_regular) < regular_count or len(available_gold) < gold_count:
                            self.message_user(
                                request,
                                f"경고: {user.username}에게 할당할 튜플이 부족합니다. 가능한만큼만 할당됩니다.",
                                level='WARNING'
                            )

                        # 할당 방식에 따라 튜플 선택
                        if assign_method == 'random':
                            selected_regular = random.sample(
                                available_regular,
                                min(regular_count, len(available_regular))
                            )
                            selected_gold = random.sample(
                                available_gold,
                                min(gold_count, len(available_gold))
                            )
                        else:  # least_rated
                            # 평가 수가 적은 순으로 정렬
                            available_regular.sort(key=lambda t: t.rating_set.count())
                            available_gold.sort(key=lambda t: t.rating_set.count())

                            selected_regular = available_regular[:min(regular_count, len(available_regular))]
                            selected_gold = available_gold[:min(gold_count, len(available_gold))]

                        # 선택된 모든 튜플
                        selected_tuples = selected_regular + selected_gold
                        random.shuffle(selected_tuples)  # 순서 섞기

                        # 할당 생성
                        for tuple_obj in selected_tuples:
                            UserWordTuple.objects.create(
                                user=user,
                                word_tuple=tuple_obj,
                                completed=False
                            )

                        total_assigned += len(selected_tuples)

                self.message_user(
                    request,
                    f"총 {len(users)}명의 작업자에게 {total_assigned}개 튜플이 할당되었습니다."
                )
                return HttpResponseRedirect("../")
        else:
            form = TupleAssignmentForm()

        # 튜플 통계 정보 수집
        from vad_survey.models import WordTuple
        tuple_stats = {
            'V': WordTuple.objects.filter(dimension='V').count(),
            'A': WordTuple.objects.filter(dimension='A').count(),
            'D': WordTuple.objects.filter(dimension='D').count(),
            'gold_V': WordTuple.objects.filter(dimension='V', is_gold=True).count(),
            'gold_A': WordTuple.objects.filter(dimension='A', is_gold=True).count(),
            'gold_D': WordTuple.objects.filter(dimension='D', is_gold=True).count(),
        }

        # 작업자 통계
        user_stats = {
            'total': User.objects.filter(is_staff=False).count(),
            'active': User.objects.filter(
                is_staff=False,
                userprofile__is_active=True
            ).count(),
        }

        return render(
            request,
            'admin/assign_tuples.html',
            {
                'form': form,
                'title': '작업자에게 튜플 할당',
                'tuple_stats': tuple_stats,
                'user_stats': user_stats,
                'opts': self.model._meta,
                'app_label': self.model._meta.app_label,
            }
        )


@staff_member_required
def assignment_dashboard(request):
    """작업 진행 상황 대시보드"""
    from django.db.models import Count, Max
    from django.db.models.functions import TruncDay
    from django.utils import timezone
    import json

    # 1. 기본 통계 정보
    word_count = Word.objects.count()
    tuple_count = WordTuple.objects.count()
    active_users = User.objects.filter(
        is_staff=False,
        userprofile__is_active=True
    ).count()

    # 2. 차원별 튜플 진행 상황
    dimension_stats = []
    for dim, dim_name in [('V', '감정가'), ('A', '각성도'), ('D', '지배성')]:
        total = WordTuple.objects.filter(dimension=dim).count()
        rated = WordTuple.objects.filter(
            dimension=dim,
            rating__isnull=False
        ).distinct().count()

        fully_rated = WordTuple.objects.annotate(
            rating_count=Count('rating', distinct=True)
        ).filter(
            dimension=dim,
            rating_count__gte=3  # V, A, D 모두 평가된 경우
        ).count()

        if total > 0:
            dimension_stats.append({
                'dimension': dim,
                'name': dim_name,
                'total': total,
                'rated': rated,
                'fully_rated': fully_rated,
                'rated_percent': round(rated / total * 100, 1),
                'fully_rated_percent': round(fully_rated / total * 100, 1),
            })

    # 3. 일별 평가 추이 (최근 2주)
    two_weeks_ago = timezone.now() - timedelta(days=14)
    daily_ratings = Rating.objects.filter(
        created_at__gte=two_weeks_ago
    ).annotate(
        day=TruncDay('created_at')
    ).values('day').annotate(
        count=Count('id')
    ).order_by('day')

    daily_chart_data = []
    for entry in daily_ratings:
        daily_chart_data.append({
            'date': entry['day'].strftime('%Y-%m-%d'),
            'count': entry['count']
        })

    # 4. 작업자별 진행 상황 (상위 20명)
    user_stats = User.objects.filter(
        is_staff=False
    ).annotate(
        assignments=Count('userwordtuple'),
        completed=Count('userwordtuple', filter=Q(userwordtuple__completed=True)),
        ratings=Count('rating'),
        last_rating=Max('rating__created_at'),
    ).order_by('-ratings')[:20]

    # 5. 전체 진행률
    progress_stats = {
        'assignments': UserWordTuple.objects.count(),
        'completed_assignments': UserWordTuple.objects.filter(completed=True).count(),
        'total_ratings': Rating.objects.count(),
        'theoretical_max_ratings': tuple_count * 3,  # 각 튜플당 V,A,D 세 개의 평가
    }

    if progress_stats['assignments'] > 0:
        progress_stats['assignment_completion'] = round(
            progress_stats['completed_assignments'] / progress_stats['assignments'] * 100, 1
        )
    else:
        progress_stats['assignment_completion'] = 0

    if progress_stats['theoretical_max_ratings'] > 0:
        progress_stats['overall_completion'] = round(
            progress_stats['total_ratings'] / progress_stats['theoretical_max_ratings'] * 100, 1
        )
    else:
        progress_stats['overall_completion'] = 0

    context = {
        'title': '작업 진행 현황 대시보드',
        'word_count': word_count,
        'tuple_count': tuple_count,
        'active_users': active_users,
        'dimension_stats': dimension_stats,
        'daily_chart_data': json.dumps(daily_chart_data),
        'user_stats': user_stats,
        'progress_stats': progress_stats,
    }

    return render(request, 'admin/assignment_dashboard.html', context)

#Rating관리
class RatingAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'user', 'get_word_tuple_display', 'display_tuple_words',
        'dimension', 'get_best_word_text', 'get_worst_word_text', 'created_at'
    )
    list_filter = ('dimension', 'created_at')
    search_fields = ('user__username', 'word_tuple__id', 'best_word__text', 'worst_word__text')
    actions = ['export_ratings_as_csv']

    def get_word_tuple_display(self, obj):
        return f"Tuple {obj.word_tuple.id}"

    get_word_tuple_display.short_description = "Word Tuple"

    def display_tuple_words(self, obj):
        return ", ".join([word.text for word in obj.word_tuple.words.all()])
    display_tuple_words.short_description = "Tuple 단어 목록"

    def get_best_word_text(self, obj):
        return obj.best_word.text if obj.best_word else "-"
    get_best_word_text.short_description = "Best Word"

    def get_worst_word_text(self, obj):
        return obj.worst_word.text if obj.worst_word else "-"
    get_worst_word_text.short_description = "Worst Word"

    @admin.action(description="선택한 평가 결과를 CSV로 내보내기")
    def export_ratings_as_csv(self, request, queryset):
        import csv
        from django.http import HttpResponse

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="ratings.csv"'

        writer = csv.writer(response)
        writer.writerow(['ID', 'User', 'Tuple ID', 'Tuple Words', 'Dimension', 'Best Word', 'Worst Word', 'Created At'])

        for rating in queryset.select_related('user', 'word_tuple', 'best_word', 'worst_word'):
            words = ", ".join([word.text for word in rating.word_tuple.words.all()])
            writer.writerow([
                rating.id,
                rating.user.username if rating.user else 'Anonymous',
                rating.word_tuple.id,
                words,
                rating.get_dimension_display(),
                rating.best_word.text if rating.best_word else '',
                rating.worst_word.text if rating.worst_word else '',
                rating.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            ])

        return response

# 기타 모델 관리자 등록
admin.site.register(Word, WordAdmin)
admin.site.register(WordTuple, WordTupleAdmin)
admin.site.register(Rating, RatingAdmin)
admin.site.register(UserProfile)
admin.site.register(UserWordTuple, UserWordTupleAdmin)
