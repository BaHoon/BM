package com.maksimowiczm.foodyou.app.ui.food.details.userproduct

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.maksimowiczm.foodyou.account.domain.FavoriteFoodIdentity
import com.maksimowiczm.foodyou.app.ui.food.details.ObserveIsFavoriteFoodUseCase
import com.maksimowiczm.foodyou.app.ui.food.details.SetFavoriteFoodUseCase
import com.maksimowiczm.foodyou.userfood.domain.product.UserProduct
import com.maksimowiczm.foodyou.userfood.domain.product.UserProductIdentity
import com.maksimowiczm.foodyou.userfood.domain.product.UserProductRepository
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

sealed interface UserProductDetailsUiEvent {
    data object Deleted : UserProductDetailsUiEvent
    data class ShowUndoDelete(val productName: String) : UserProductDetailsUiEvent
}

internal class UserProductDetailsViewModel(
    private val identity: UserProductIdentity,
    private val userProductRepository: UserProductRepository,
    observeIsFavoriteFoodUseCase: ObserveIsFavoriteFoodUseCase,
    private val setFavoriteFoodUseCase: SetFavoriteFoodUseCase,
) : ViewModel() {
    private val eventChannel = Channel<UserProductDetailsUiEvent>()
    val uiEvents = eventChannel.receiveAsFlow()

    private var pendingDeleteJob: Job? = null
    private var pendingDeleteProduct: UserProduct? = null

    val isFavorite =
        observeIsFavoriteFoodUseCase
            .observe(identity)
            .stateIn(
                scope = viewModelScope,
                started = SharingStarted.WhileSubscribed(2_000),
                initialValue = null,
            )

    val userFood =
        userProductRepository
            .observe(identity)
            .stateIn(
                scope = viewModelScope,
                started = SharingStarted.WhileSubscribed(2_000),
                initialValue = null,
            )

    fun setFavorite(isFavorite: Boolean) {
        viewModelScope.launch {
            setFavoriteFoodUseCase.setFavoriteFood(
                identity = FavoriteFoodIdentity.UserProduct(identity.id),
                isFavorite = isFavorite,
            )
        }
    }

    fun delete() {
        viewModelScope.launch {
            // 取消之前的删除操作
            pendingDeleteJob?.cancel()
            
            // 获取完整的产品数据
            val product = userProductRepository.observe(identity).first()
            if (product == null) return@launch
            
            pendingDeleteProduct = product
            
            // 发送UI事件显示Snackbar
            eventChannel.send(UserProductDetailsUiEvent.ShowUndoDelete(product.name))
            
            // 延迟5秒后执行实际删除
            pendingDeleteJob = launch {
                delay(5000)
                userProductRepository.delete(identity)
                pendingDeleteProduct = null
                eventChannel.send(UserProductDetailsUiEvent.Deleted)
            }
        }
    }

    fun undoDelete() {
        viewModelScope.launch {
            // 取消待执行的删除
            pendingDeleteJob?.cancel()
            pendingDeleteProduct = null
        }
    }
}
