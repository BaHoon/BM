package com.maksimowiczm.foodyou.app.ui.profile.edit

import androidx.compose.foundation.text.input.TextFieldState
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import co.touchlab.kermit.Logger
import com.maksimowiczm.foodyou.account.domain.Account
import com.maksimowiczm.foodyou.account.domain.AccountRepository
import com.maksimowiczm.foodyou.account.domain.Profile
import com.maksimowiczm.foodyou.app.application.AppAccountManager
import com.maksimowiczm.foodyou.app.ui.common.component.ProfileAvatarMapper
import com.maksimowiczm.foodyou.app.ui.common.component.UiProfileAvatar
import com.maksimowiczm.foodyou.app.ui.profile.ProfileUiState
import com.maksimowiczm.foodyou.common.domain.ProfileId
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

class EditProfileViewModel(
    private val profileId: ProfileId,
    private val appAccountManager: AppAccountManager,
    private val accountRepository: AccountRepository,
    logger: Logger,
) : ViewModel() {
    private val logger = logger.withTag(TAG)

    private val _uiState = MutableStateFlow(ProfileUiState())
    val uiState: StateFlow<ProfileUiState> = _uiState.asStateFlow()

    private var pendingDeleteJob: Job? = null
    private var pendingDeleteProfile: Profile? = null

    val canDelete: StateFlow<Boolean> =
        appAccountManager
            .observeAppAccount()
            .map { it.profiles.size > 1 }
            .stateIn(
                scope = viewModelScope,
                started = SharingStarted.WhileSubscribed(2000),
                initialValue = false,
            )

    private val _uiEventBus = Channel<EditProfileEvent>()
    val uiEvents = _uiEventBus.receiveAsFlow()

    init {
        viewModelScope.launch {
            logger.d { "Loading primary account to edit profile" }
            val account = appAccountManager.observeAppAccount().first()
            val profile = account.profiles.find { it.id == profileId }

            if (profile != null) {
                _uiState.value =
                    ProfileUiState(
                        nameTextState = TextFieldState(profile.name),
                        avatar = ProfileAvatarMapper.toUiModel(profile.avatar),
                        defaultName = profile.name,
                        defaultAvatar = ProfileAvatarMapper.toUiModel(profile.avatar),
                    )
            } else {
                error(
                    "Profile with id $profileId not found in primary account ${account.localAccountId}"
                )
            }
        }
    }

    fun setAvatar(avatar: UiProfileAvatar) {
        _uiState.value = _uiState.value.copy(avatar = avatar)
    }

    fun edit() {
        val state = _uiState.value
        if (state.isLocked) {
            logger.w { "Create profile called while already locked" }
            return
        }

        _uiState.value = state.copy(isLocked = true)

        viewModelScope.launch {
            val account = appAccountManager.observeAppAccount().first()

            account.updateProfile(profileId) {
                it.apply {
                    updateName(state.nameTextState.text.toString())
                    updateAvatar(ProfileAvatarMapper.toModel(state.avatar))
                }
            }

            accountRepository.save(account)

            _uiEventBus.send(EditProfileEvent.Edited)
        }
    }

    fun delete() {
        val state = _uiState.value
        if (state.isLocked) {
            logger.w { "Delete profile called while already locked" }
            return
        }

        _uiState.value = state.copy(isLocked = true)

        viewModelScope.launch {
            val account = appAccountManager.observeAppAccount().first()
            val profile = account.profiles.find { it.id == profileId }
            
            if (profile == null) {
                logger.w { "Profile not found for delete" }
                _uiState.value = state.copy(isLocked = false)
                return@launch
            }
            
            // 取消之前的删除操作
            pendingDeleteJob?.cancel()
            
            pendingDeleteProfile = profile
            
            // 发送UI事件显示Snackbar
            _uiEventBus.send(EditProfileEvent.ShowUndoDelete(profile.name))
            
            // 延迟5秒后执行实际删除
            pendingDeleteJob = launch {
                delay(5000)
                val currentAccount = appAccountManager.observeAppAccount().first()
                currentAccount.removeProfile(profileId)
                accountRepository.save(currentAccount)
                pendingDeleteProfile = null
                _uiEventBus.send(EditProfileEvent.Deleted)
            }
        }
    }

    fun undoDelete() {
        viewModelScope.launch {
            // 取消待执行的删除
            pendingDeleteJob?.cancel()
            
            // 重置锁定状态
            _uiState.value = _uiState.value.copy(isLocked = false)
            
            pendingDeleteProfile = null
        }
    }

    private companion object {
        private const val TAG = "EditProfileViewModel"
    }
}
