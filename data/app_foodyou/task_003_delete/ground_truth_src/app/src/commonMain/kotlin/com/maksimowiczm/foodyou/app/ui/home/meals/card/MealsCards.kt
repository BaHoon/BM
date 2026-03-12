package com.maksimowiczm.foodyou.app.ui.home.meals.card

import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.material3.SnackbarDuration
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SnackbarResult
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.maksimowiczm.foodyou.app.ui.home.shared.HomeState
import com.maksimowiczm.foodyou.common.compose.extension.LaunchedCollectWithLifecycle
import com.maksimowiczm.foodyou.fooddiary.domain.entity.MealsCardsLayout
import foodyou.app.generated.resources.*
import kotlinx.coroutines.launch
import org.jetbrains.compose.resources.getString
import org.koin.compose.viewmodel.koinViewModel

@Composable
internal fun MealsCards(
    homeState: HomeState,
    onAdd: (epochDay: Long, mealId: Long) -> Unit,
    onQuickAdd: (epochDay: Long, mealId: Long) -> Unit,
    onEditEntry: (foodEntryId: Long?, manualEntryId: Long?) -> Unit,
    onLongClick: (mealId: Long) -> Unit,
    contentPadding: PaddingValues,
    snackbarHostState: SnackbarHostState,
    modifier: Modifier = Modifier,
) {
    val viewModel: MealsCardsViewModel = koinViewModel()
    val diaryMeals = viewModel.diaryMeals.collectAsStateWithLifecycle().value
    val layout by viewModel.layout.collectAsStateWithLifecycle()

    val scope = rememberCoroutineScope()
    
    LaunchedCollectWithLifecycle(viewModel.uiEvents) { event ->
        when (event) {
            is MealsCardsUiEvent.ShowUndoDelete -> {
                val message = getString(Res.string.notification_entry_deleted, event.entryName)
                val actionLabel = getString(Res.string.action_undo)
                val result = snackbarHostState.showSnackbar(
                    message = message,
                    actionLabel = actionLabel,
                    duration = SnackbarDuration.Short
                )
                if (result == SnackbarResult.ActionPerformed) {
                    viewModel.undoDelete()
                }
            }
        }
    }

    LaunchedEffect(homeState.selectedDate, viewModel) { viewModel.setDate(homeState.selectedDate) }

    when (layout) {
        MealsCardsLayout.Horizontal ->
            HorizontalMealsCards(
                meals = diaryMeals,
                onAdd = { mealId -> onAdd(homeState.selectedDate.toEpochDays(), mealId) },
                onQuickAdd = { mealId -> onQuickAdd(homeState.selectedDate.toEpochDays(), mealId) },
                onEditEntry = { model ->
                    val foodEntry = model as? FoodMealEntryModel
                    val manualEntry = model as? ManualMealEntryModel
                    onEditEntry(foodEntry?.id?.value, manualEntry?.id?.value)
                },
                onDeleteEntry = viewModel::onDeleteEntry,
                onLongClick = onLongClick,
                shimmer = homeState.shimmer,
                contentPadding = contentPadding,
                modifier = modifier,
            )

        MealsCardsLayout.Vertical ->
            VerticalMealsCards(
                meals = diaryMeals,
                onAdd = { mealId -> onAdd(homeState.selectedDate.toEpochDays(), mealId) },
                onQuickAdd = { mealId -> onQuickAdd(homeState.selectedDate.toEpochDays(), mealId) },
                onEditEntry = { model ->
                    val foodEntry = model as? FoodMealEntryModel
                    val manualEntry = model as? ManualMealEntryModel
                    onEditEntry(foodEntry?.id?.value, manualEntry?.id?.value)
                },
                onDeleteEntry = viewModel::onDeleteEntry,
                onLongClick = onLongClick,
                shimmer = homeState.shimmer,
                contentPadding = contentPadding,
                modifier = modifier,
            )
    }
}
