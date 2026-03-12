package com.maksimowiczm.foodyou.app.ui.meal

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.maksimowiczm.foodyou.fooddiary.domain.entity.Meal
import com.maksimowiczm.foodyou.fooddiary.domain.repository.MealRepository
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

sealed interface MealSettingsUiEvent {
    data class ShowUndoDelete(val mealName: String) : MealSettingsUiEvent
}

internal class MealSettingsViewModel(private val mealRepository: MealRepository) : ViewModel() {

    private val _uiEvents = Channel<MealSettingsUiEvent>()
    val uiEvents = _uiEvents.receiveAsFlow()

    private var pendingDeleteJob: Job? = null
    private var pendingDeleteMeal: Meal? = null

    val meals: StateFlow<List<MealModel>?> =
        mealRepository
            .observeMeals()
            .distinctUntilChanged()
            .map { meals ->
                meals
                    .sortedBy { it.rank }
                    .map { meal ->
                        MealModel(
                            id = meal.id,
                            name = meal.name,
                            from = meal.from,
                            to = meal.to,
                            isAllDay = meal.from == meal.to,
                        )
                    }
            }
            .stateIn(
                scope = viewModelScope,
                started = SharingStarted.WhileSubscribed(2_000),
                initialValue = null,
            )

    fun deleteMeal(mealModel: MealModel) {
        viewModelScope.launch {
            // 取消之前的删除操作
            pendingDeleteJob?.cancel()
            
            // 获取完整的meal数据
            val meal = mealRepository.observeMeal(mealModel.id).first()
            if (meal == null) return@launch
            
            pendingDeleteMeal = meal
            
            // 发送UI事件显示Snackbar
            _uiEvents.send(MealSettingsUiEvent.ShowUndoDelete(meal.name))
            
            // 延迟5秒后执行实际删除
            pendingDeleteJob = launch {
                delay(5000)
                mealRepository.deleteMeal(mealModel.id)
                pendingDeleteMeal = null
            }
        }
    }

    fun undoDelete() {
        viewModelScope.launch {
            // 取消待执行的删除
            pendingDeleteJob?.cancel()
            
            val meal = pendingDeleteMeal ?: return@launch
            
            // 重新插入meal
            mealRepository.insertMealWithLastRank(
                name = meal.name,
                from = meal.from,
                to = meal.to
            )
            
            pendingDeleteMeal = null
        }
    }

    fun updateMeal(mealModel: MealModel) {
        viewModelScope.launch {
            mealRepository.updateMeal(
                id = mealModel.id,
                name = mealModel.name,
                from = mealModel.from,
                to = mealModel.to,
            )
        }
    }

    fun createMeal(mealModel: MealModel) {
        viewModelScope.launch {
            mealRepository.insertMealWithLastRank(
                name = mealModel.name,
                from = mealModel.from,
                to = mealModel.to,
            )
        }
    }

    fun updateMealOrder(mealModels: List<MealModel>) {
        viewModelScope.launch { mealRepository.reorderMeals(mealModels.map { it.id }) }
    }
}
