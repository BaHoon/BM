package com.maksimowiczm.foodyou.app.ui.home.meals.card

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.maksimowiczm.foodyou.common.domain.userpreferences.UserPreferencesRepository
import com.maksimowiczm.foodyou.fooddiary.domain.entity.DiaryEntry
import com.maksimowiczm.foodyou.fooddiary.domain.entity.DiaryFoodRecipe
import com.maksimowiczm.foodyou.fooddiary.domain.entity.DiaryMeal
import com.maksimowiczm.foodyou.fooddiary.domain.entity.FoodDiaryEntry
import com.maksimowiczm.foodyou.fooddiary.domain.entity.ManualDiaryEntry
import com.maksimowiczm.foodyou.fooddiary.domain.entity.MealsPreferences
import com.maksimowiczm.foodyou.fooddiary.domain.repository.FoodDiaryEntryRepository
import com.maksimowiczm.foodyou.fooddiary.domain.repository.ManualDiaryEntryRepository
import com.maksimowiczm.foodyou.fooddiary.domain.usecase.ObserveDiaryMealsUseCase
import kotlin.math.roundToInt
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.datetime.LocalDate

sealed interface MealsCardsUiEvent {
    data class ShowUndoDelete(val entryName: String) : MealsCardsUiEvent
}

internal class MealsCardsViewModel(
    private val observeDiaryMealsUseCase: ObserveDiaryMealsUseCase,
    private val foodEntryRepository: FoodDiaryEntryRepository,
    private val manualEntryRepository: ManualDiaryEntryRepository,
    mealsPreferencesRepository: UserPreferencesRepository<MealsPreferences>,
) : ViewModel() {
    private val dateState = MutableStateFlow<LocalDate?>(null)

    private val _uiEvents = Channel<MealsCardsUiEvent>()
    val uiEvents = _uiEvents.receiveAsFlow()

    private var pendingDeleteJob: Job? = null
    private var pendingDeleteEntry: DiaryEntry? = null

    val diaryMeals: StateFlow<List<MealModel>?> =
        dateState
            .filterNotNull()
            .flatMapLatest { date -> observeDiaryMealsUseCase.observe(date) }
            .map { list -> list.map { it.toMealModel() } }
            .stateIn(
                scope = viewModelScope,
                started = SharingStarted.WhileSubscribed(60_000),
                initialValue = null,
            )

    private val _layout = mealsPreferencesRepository.observe().map { it.layout }
    val layout =
        _layout.stateIn(
            scope = viewModelScope,
            started = SharingStarted.WhileSubscribed(2_000),
            initialValue = runBlocking { _layout.first() },
        )

    fun setDate(date: LocalDate) {
        viewModelScope.launch { dateState.value = date }
    }

    fun onDeleteEntry(model: MealEntryModel) {
        viewModelScope.launch {
            // 取消之前的删除操作
            pendingDeleteJob?.cancel()
            
            // 获取完整的条目数据用于可能的撤销
            val entry = when (model) {
                is FoodMealEntryModel -> foodEntryRepository.observe(model.id).first()
                is ManualMealEntryModel -> manualEntryRepository.observe(model.id).first()
            }
            
            if (entry == null) return@launch
            
            pendingDeleteEntry = entry
            
            // 发送UI事件显示Snackbar
            _uiEvents.send(MealsCardsUiEvent.ShowUndoDelete(entry.name))
            
            // 延迟5秒后执行实际删除
            pendingDeleteJob = launch {
                delay(5000)
                when (model) {
                    is FoodMealEntryModel -> foodEntryRepository.delete(model.id)
                    is ManualMealEntryModel -> manualEntryRepository.delete(model.id)
                }
                pendingDeleteEntry = null
            }
        }
    }

    fun undoDelete() {
        viewModelScope.launch {
            // 取消待执行的删除
            pendingDeleteJob?.cancel()
            
            val entry = pendingDeleteEntry ?: return@launch
            
            // 重新插入条目
            when (entry) {
                is FoodDiaryEntry -> {
                    foodEntryRepository.insert(
                        measurement = entry.measurement,
                        mealId = entry.mealId,
                        date = entry.date,
                        food = entry.food,
                        createdAt = entry.createdAt
                    )
                }
                is ManualDiaryEntry -> {
                    manualEntryRepository.insert(
                        name = entry.name,
                        mealId = entry.mealId,
                        date = entry.date,
                        nutritionFacts = entry.nutritionFacts,
                        createdAt = entry.createdAt
                    )
                }
            }
            
            pendingDeleteEntry = null
        }
    }
}

private fun DiaryMeal.toMealModel(): MealModel =
    MealModel(
        id = meal.id,
        name = meal.name,
        from = meal.from,
        to = meal.to,
        isAllDay = meal.from == meal.to,
        foods = entries.map { it.toMealEntryModel() },
        energy = nutritionFacts.energy.value?.roundToInt() ?: 0,
        proteins = nutritionFacts.proteins.value ?: 0.0,
        carbohydrates = nutritionFacts.carbohydrates.value ?: 0.0,
        fats = nutritionFacts.fats.value ?: 0.0,
    )

private fun DiaryEntry.toMealEntryModel(): MealEntryModel =
    when (this) {
        is FoodDiaryEntry ->
            FoodMealEntryModel(
                id = id,
                name = food.name,
                energy = nutritionFacts.energy.value?.roundToInt(),
                proteins = nutritionFacts.proteins.value,
                carbohydrates = nutritionFacts.carbohydrates.value,
                fats = nutritionFacts.fats.value,
                measurement = measurement,
                weight = weight,
                isLiquid = food.isLiquid,
                isRecipe = food is DiaryFoodRecipe,
                totalWeight = food.totalWeight,
                servingWeight = food.servingWeight,
            )

        is ManualDiaryEntry ->
            ManualMealEntryModel(
                id = id,
                name = name,
                energy = nutritionFacts.energy.value?.roundToInt(),
                proteins = nutritionFacts.proteins.value,
                carbohydrates = nutritionFacts.carbohydrates.value,
                fats = nutritionFacts.fats.value,
            )
    }
